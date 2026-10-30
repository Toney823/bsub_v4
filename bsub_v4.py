#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bsub_v4.py - LSF Batch Submission and Command Console

A lightweight command-line frontend for IBM Spectrum LSF. It provides two
working modes: the original script-submission interface and an interactive
command console that submits each entered command as a separate batch job.
The console is batch-backed; it does not create a persistent remote shell or
a pseudo-terminal.
"""

import argparse
import copy
import datetime as dt
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

try:
    import readline
except ImportError:
    readline = None

try:
    import termios
except ImportError:
    termios = None

INTERFACE_DIR = Path.home() / ".interface"
JOBS_DIR = INTERFACE_DIR / "jobs"
TERMINAL_STATES = {"DONE", "EXIT", "ZOMBI"}


def run_capture(command, **kwargs):
    return subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, errors="replace", **kwargs)


def openfile(path, nohash="True"):
    """Keep the v3 behavior: remove blank lines and (optionally) full-line comments."""
    lines = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle.read().splitlines():
            if not line.strip():
                continue
            if nohash == "True" and line.startswith("#"):
                continue
            lines.append(line)
    return lines


def bmgroup_rows():
    result = run_capture(["bmgroup", "-w"])
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "bmgroup -w 执行失败")
    rows = [line.split() for line in result.stdout.splitlines() if line.strip()]
    data = [row for row in rows if row[0] != "GROUP_NAME"]
    if not data:
        raise RuntimeError("bmgroup -w 未找到可用节点组")
    return data


def pick_node(row):
    if len(row) < 2:
        raise RuntimeError("该节点组没有子节点")
    hosts = [host for token in row[1:] for host in token.split(",") if host]
    result = run_capture(["bhosts", "-w"])
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "bhosts -w 执行失败")
    host_info = {}
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 5 and fields[0] != "HOST_NAME":
            host_info[fields[0]] = (fields[1], fields[4])
    print("子节点负载 (NJOBS): " + ", ".join(
        "%s:%s" % (host, host_info[host][1] if host in host_info else "?")
        for host in hosts))
    ok_nodes = [(host, int(host_info[host][1])) for host in hosts
                if host in host_info and host_info[host][0] == "ok"
                and host_info[host][1].isdigit()]
    if not ok_nodes:
        raise RuntimeError("所选节点组中没有状态为 ok 的可用节点")
    best = min(ok_nodes, key=lambda item: item[1])
    print("选择 NJOBS 最少的节点: %s (NJOBS=%d)" % best)
    return best[0]


def select_machine(partitions, allow_prompt=True):
    data = bmgroup_rows()
    for token in partitions:
        for row in data:
            if row[0] in (token, token + "_hosts"):
                print("根据 -p %s 匹配到节点组: %s" % (token, row[0]))
                return pick_node(row)
    if not allow_prompt:
        raise RuntimeError("bmgroup -w 中未找到 -p 对应的节点组；-y 模式不支持交互选择，请用 -m 指定节点")
    print("可用节点组:")
    for index, row in enumerate(data, 1):
        print("  %d. %s -> %s" % (index, row[0], " ".join(row[1:])))
    raw = input("请输入节点组序号或组名: ").strip()
    try:
        row = data[int(raw) - 1] if raw.isdigit() else next(r for r in data if r[0] == raw)
    except (ValueError, IndexError, StopIteration):
        raise RuntimeError("节点组输入有误")
    return pick_node(row)


def validate_common(job_name, queue, cpu):
    if cpu < 1:
        raise ValueError("CPU 数必须大于等于 1")
    for label, value in (("任务名", job_name), ("队列名", queue)):
        if not value or re.search(r"[\s\r\n]", value):
            raise ValueError("%s不能为空且不能包含空白字符" % label)


def make_bsub_script(job_name, queue, cpu, out_path, err_path, cwd, body, machine=None):
    validate_common(job_name, queue, cpu)
    lines = [
        "#!/bin/bash",
        "#BSUB -J " + job_name,
        "#BSUB -q " + queue,
        "#BSUB -n " + str(cpu),
    ]
    if machine:
        if re.search(r"[\s\r\n]", machine):
            raise ValueError("计算节点名称不能包含空白字符")
        lines.append("#BSUB -m " + machine)
    # Keep the directive order consistent with the original v3 script.
    lines += [
        "#BSUB -e " + str(err_path),
        "#BSUB -o " + str(out_path),
        "cd -- " + shlex.quote(str(cwd)) + " || exit 125",
        body.rstrip("\n"),
        "",
    ]
    return "\n".join(lines)


def call_bsub(script_path):
    """Submit a script and return (job_id, combined_message, return_code)."""
    command = ["bsub"]
    submit_env = os.environ.copy()
    # Avoid LSF completion-mail noise for this command-console mode.
    submit_env["LSB_JOB_REPORT_MAIL"] = "N"
    try:
        with script_path.open("r", encoding="utf-8") as handle:
            result = subprocess.run(command, stdin=handle, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, errors="replace",
                                    env=submit_env)
    except FileNotFoundError:
        raise RuntimeError("找不到 bsub；请先加载 LSF 环境")
    message = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
    match = re.search(r"Job\s*<([0-9]+)>", message)
    if result.returncode != 0 or not match:
        raise RuntimeError(message or "bsub 提交失败，且未返回 Job ID")
    return match.group(1), message, result.returncode


def query_job_status(job_id):
    result = run_capture(["bjobs", "-a", "-noheader", "-o", "stat", job_id])
    text = (result.stdout or "") + "\n" + (result.stderr or "")
    for token in re.findall(r"\b(?:PEND|PSUSP|RUN|SSUSP|USUSP|DONE|EXIT|ZOMBI|UNKWN)\b", text.upper()):
        return token
    return None


def wait_for_job(job_id, poll_interval=2, ignore_interrupts=False):
    last_status = None
    missing = 0
    print("[等待] Job <%s>" % job_id, file=sys.stderr, flush=True)
    while True:
        try:
            status = query_job_status(job_id)
        except KeyboardInterrupt:
            if not ignore_interrupts:
                raise
            print("\n取消请求已发送，继续等待 LSF 确认作业结束。",
                  file=sys.stderr, flush=True)
            continue
        except FileNotFoundError:
            raise RuntimeError("找不到 bjobs，无法确认作业状态")
        if status:
            missing = 0
            if status != last_status:
                print("[状态] %s" % status, file=sys.stderr, flush=True)
                last_status = status
            if status in TERMINAL_STATES:
                return status
        else:
            missing += 1
            if missing >= 15:
                raise RuntimeError("bjobs 连续无法查询到 Job <%s>；请用 bjobs/bhist 核实作业状态" % job_id)
        try:
            time.sleep(poll_interval)
        except KeyboardInterrupt:
            if not ignore_interrupts:
                raise
            print("\n作业仍在终止过程中，继续等待；不会提交下一条命令。",
                  file=sys.stderr, flush=True)


def extract_lsf_payload(text):
    """Strip LSF's default job-report envelope and return only command output."""
    if not text:
        return ""
    marker = "The output (if any) follows:"
    if marker not in text:
        return "" if text.lstrip().startswith("Sender: LSF System") else text
    payload = text.split(marker, 1)[1].lstrip("\r\n")
    footer = re.search(
        r"(?m)^PS:\s*\r?\nRead file <.*?> for stderr output of this job\.\s*$",
        payload,
    )
    if footer:
        payload = payload[:footer.start()]
    return payload.rstrip("\r\n") + ("\n" if payload.strip() else "")


def print_job_logs(out_path, err_path, job_id, status):
    print("\n[Job <%s>] %s" % (job_id, status), file=sys.stderr, flush=True)
    try:
        raw_out = out_path.read_text(encoding="utf-8", errors="replace") if out_path.exists() else ""
        raw_err = err_path.read_text(encoding="utf-8", errors="replace") if err_path.exists() else ""
        out_text = extract_lsf_payload(raw_out)
        err_text = extract_lsf_payload(raw_err)
    except OSError as exc:
        print("读取日志失败: %s" % exc, file=sys.stderr)
        return
    if out_text:
        sys.stdout.write(out_text)
        if not out_text.endswith("\n"):
            sys.stdout.write("\n")
        sys.stdout.flush()
    else:
        print("[stdout 无输出]", file=sys.stderr)
    if err_text:
        sys.stderr.write(err_text)
        if not err_text.endswith("\n"):
            sys.stderr.write("\n")
        sys.stderr.flush()
    print("[日志] stdout: %s" % out_path, file=sys.stderr)
    print("[日志] stderr: %s" % err_path, file=sys.stderr)


def submit_legacy(args):
    validate_common(args.jobName, args.partition, args.cpuNumber)
    if not Path(args.scriptName).is_file():
        raise RuntimeError("脚本不存在: %s" % args.scriptName)
    machine = None
    if args.machine is not None:
        if args.machine == "":
            partitions = [p.strip() for p in args.partition.split(",") if p.strip()]
            machine = select_machine(partitions, allow_prompt=not args.yes)
        else:
            machine = args.machine
    stem = args.jobName + ".sh"
    script_path = Path.cwd() / stem
    out_path = Path.cwd() / (args.jobName + ".out")
    err_path = Path.cwd() / (args.jobName + ".err")
    content = "\n".join(openfile(args.scriptName, nohash="True"))
    script = make_bsub_script(args.jobName, args.partition, args.cpuNumber,
                              out_path, err_path, Path.cwd(), content, machine)
    if not args.yes:
        print(script)
        try:
            input("\nok，提交请敲 Enter，取消请按 Ctrl+C：")
        except KeyboardInterrupt:
            print("\n已取消提交。")
            return
    script_path.write_text(script + "\n", encoding="utf-8")
    try:
        with script_path.open("r", encoding="utf-8") as handle:
            result = subprocess.run(["bsub"], stdin=handle, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, errors="replace")
    except FileNotFoundError:
        raise RuntimeError("找不到 bsub；请先加载 LSF 环境")
    message = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
    if message:
        print(message)
    if result.returncode != 0:
        raise RuntimeError("bsub 提交失败，返回码 %d" % result.returncode)
    print("提交脚本: %s" % script_path)


class TerminalInputFix:
    """Make Ctrl-H and DEL behave as Backspace, then restore the terminal on exit."""
    def __init__(self):
        self.original = None
        self.fd = None

    def __enter__(self):
        if readline is not None:
            try:
                readline.parse_and_bind("set editing-mode emacs")
                readline.parse_and_bind(r'"\C-h": backward-delete-char')
                readline.parse_and_bind(r'"\C-?": backward-delete-char')
                return self
            except Exception:
                pass
        if termios is not None and sys.stdin.isatty():
            try:
                self.fd = sys.stdin.fileno()
                self.original = termios.tcgetattr(self.fd)
                updated = copy.deepcopy(self.original)
                updated[6][termios.VERASE] = b"\x08"
                termios.tcsetattr(self.fd, termios.TCSANOW, updated)
            except (OSError, termios.error):
                self.original = None
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self.original is not None and self.fd is not None:
            try:
                termios.tcsetattr(self.fd, termios.TCSANOW, self.original)
            except (OSError, termios.error):
                pass


class InteractConsole:
    def __init__(self, cpu, job_name, queue, environment, first_line):
        self.cpu = cpu
        self.job_name = job_name
        self.queue = queue
        self.environment = environment.strip()
        self.cwd = Path.cwd().resolve()
        self.previous_cwd = self.cwd
        self.first_line = first_line
        self.counter = 0
        self.session = dt.datetime.now().strftime("%Y%m%d_%H%M%S") + "_%d" % os.getpid()
        self.session_dir = JOBS_DIR / self.session
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.init_file = INTERFACE_DIR / "init.sh"

    def prompt(self):
        env = self.environment if self.environment else "no-conda-env"
        return "[%s/%s/%s] %s $ " % (self.queue, self.cpu, env, self.cwd)

    def local_command(self, command):
        try:
            args = shlex.split(command)
        except ValueError as exc:
            print("命令解析错误: %s" % exc, file=sys.stderr)
            return True
        if not args:
            return True
        if args[0] in ("exit", "quit"):
            raise EOFError
        if args[0] in ("help", "?"):
            print("普通命令：每行提交一个普通 LSF 批处理作业；exit/quit 退出。")
            print("cd 在登录节点更新会话工作目录；-e 指定的 Conda 环境会在每个作业中重新激活。")
            print("日志目录: %s" % self.session_dir)
            return True
        if args[0] == "cd":
            if len(args) > 2:
                print("用法: cd [目录]", file=sys.stderr)
                return True
            target = args[1] if len(args) == 2 else str(Path.home())
            if target == "-":
                target = str(self.previous_cwd)
            target = os.path.expandvars(os.path.expanduser(target))
            path = Path(target)
            if not path.is_absolute():
                path = self.cwd / path
            try:
                path = path.resolve(strict=True)
                if not path.is_dir():
                    raise NotADirectoryError(str(path))
                self.previous_cwd, self.cwd = self.cwd, path
            except OSError as exc:
                print("cd: %s" % exc, file=sys.stderr)
            return True
        return False

    def build_body(self, command):
        lines = []
        if self.init_file.is_file():
            lines.append("source %s || exit 125" % shlex.quote(str(self.init_file)))
        # If only the conda executable is on PATH, initialize its Bash function.
        lines += [
            "if ! type conda >/dev/null 2>&1 && command -v conda >/dev/null 2>&1; then",
            "  eval \"$(conda shell.bash hook 2>/dev/null)\" || true",
            "fi",
        ]
        if self.environment:
            lines += [
                "if ! type conda >/dev/null 2>&1; then",
                "  echo '错误: conda 命令不可用；请检查计算节点 PATH 或 ~/.interface/init.sh' >&2",
                "  exit 127",
                "fi",
                "conda activate %s || exit $?" % shlex.quote(self.environment),
            ]
        lines.append(command)
        return "\n".join(lines)

    def update_conda_state(self, command, status):
        """Track simple conda activate/deactivate commands for later batch jobs."""
        if status != "DONE":
            return
        try:
            tokens = shlex.split(command)
        except ValueError:
            return
        if tokens[:2] == ["conda", "activate"] and len(tokens) in (2, 3):
            self.environment = tokens[2] if len(tokens) == 3 else "base"
            print("[环境] 后续作业将自动激活 Conda 环境: %s" % self.environment,
                  file=sys.stderr, flush=True)
        elif tokens[:2] == ["conda", "deactivate"] and len(tokens) == 2:
            self.environment = ""
            print("[环境] 后续作业不再自动激活 Conda 环境。",
                  file=sys.stderr, flush=True)

    def submit_command(self, command):
        self.counter += 1
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        stem = "%04d_%s" % (self.counter, stamp)
        safe_job = re.sub(r"[^A-Za-z0-9_.-]+", "_", self.job_name)[:30] or "interact"
        job_name = "%s_%d_%04d" % (safe_job, os.getpid(), self.counter)
        script_path = self.session_dir / (stem + ".sh")
        out_path = self.session_dir / (stem + ".out")
        err_path = self.session_dir / (stem + ".err")
        script = make_bsub_script(job_name, self.queue, self.cpu, out_path, err_path,
                                  self.cwd, self.build_body(command))
        script_path.write_text(script + "\n", encoding="utf-8")
        print("\n[提交] %s" % command, file=sys.stderr, flush=True)
        try:
            job_id, message, _ = call_bsub(script_path)
        except RuntimeError as exc:
            print("提交失败: %s" % exc, file=sys.stderr)
            return
        print("[LSF] Job <%s> 已提交" % job_id, file=sys.stderr, flush=True)
        try:
            status = wait_for_job(job_id)
        except KeyboardInterrupt:
            print("\n收到 Ctrl+C，尝试取消 Job <%s>..." % job_id, file=sys.stderr)
            killed = run_capture(["bkill", job_id])
            if killed.stdout.strip() or killed.stderr.strip():
                print((killed.stdout + killed.stderr).strip(), file=sys.stderr)
            try:
                status = wait_for_job(job_id, ignore_interrupts=True)
            except RuntimeError as exc:
                print(str(exc), file=sys.stderr)
                return
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            print("日志文件: %s ; %s" % (out_path, err_path), file=sys.stderr)
            return
        print_job_logs(out_path, err_path, job_id, status)
        self.update_conda_state(command, status)

    def run(self):
        print("LSF 批处理命令模式：每条命令独立提交，完成后回显 stdout/stderr。")
        print("队列=%s CPU=%d Conda环境=%s" %
              (self.queue, self.cpu, self.environment or "(不激活)"))
        print("输入 exit/quit 退出；cd 在登录节点维护会话工作目录。")
        print("作业脚本和日志: %s" % self.session_dir)
        with TerminalInputFix():
            if self.first_line:
                self.submit_command(self.first_line)
            while True:
                try:
                    command = input(self.prompt())
                    if command.strip() and not self.local_command(command):
                        self.submit_command(command)
                except EOFError:
                    print()
                    return
                except KeyboardInterrupt:
                    print("\n当前输入已取消。", file=sys.stderr, flush=True)


def print_main_help():
    parser = argparse.ArgumentParser(
        prog="bsub_v4.py",
        usage=(
            "%(prog)s -s SCRIPT -n NAME -p QUEUE -c CPU [-m [HOST]] [-y]\n"
            "       %(prog)s interact -c CPU -n NAME -p QUEUE [-e ENV] -l COMMAND\n"
            "       %(prog)s -h"
        ),
        description=(
            "A lightweight command-line frontend for IBM Spectrum LSF. "
            "It supports the original script-submission interface and a "
            "batch-backed command console."
        ),
        formatter_class=argparse.RawTextHelpFormatter,
        epilog=(
            "MODES:\n"
            "  Default mode  Submit an existing shell script (compatible with bsub_v3.py).\n"
            "  interact      Start a command console; each command is submitted as a\n"
            "                separate LSF batch job and its stdout/stderr are displayed.\n"
            "                The console remains open until 'exit', 'quit', or Ctrl-D.\n"
            "\n"
            "DEFAULT SUBMISSION OPTIONS:\n"
            "  -s, --scriptName SCRIPT   Shell script to submit.\n"
            "  -n, --jobName NAME        Job name.\n"
            "  -p, --partition QUEUE     LSF queue.\n"
            "  -c, --cpuNumber CPU       CPU cores requested.\n"
            "  -m, --machine [HOST]      Optional host; an empty -m selects a host from bmgroup.\n"
            "  -y, --yes                 Submit without confirmation.\n"
            "\n"
            "INTERACT OPTIONS:\n"
            "  -c, --cpuNumber CPU       CPU cores requested per command job.\n"
            "  -n, --jobName NAME        Job-name prefix.\n"
            "  -p, --partition QUEUE     LSF queue.\n"
            "  -e, --environment ENV     Optional Conda environment name or path; default: empty.\n"
            "  -l, --line COMMAND        First command to execute (quote the command).\n"
            "\n"
            "EXAMPLES:\n"
            "  bsub_v4.py -s run.sh -n test -p fatA -c 4 -y\n"
            "  bsub_v4.py interact -c 1 -n dev -p fatA -l 'hostname'\n"
            "  bsub_v4.py interact -c 1 -n dev -p fatA -e esmfold -l 'python --version'\n"
            "  bsub_v4.py interact -h\n"
            "\n"
            "NOTE: interact is a sequence of independent batch jobs, not a persistent\n"
            "remote shell or a TTY/PTY session. Shell state such as arbitrary variables\n"
            "and exports is not automatically preserved between jobs."
        ),
    )
    parser.print_help()


def parse_legacy(argv):
    parser = argparse.ArgumentParser(
        description="Submit an existing shell script to an IBM Spectrum LSF batch queue.")
    parser.add_argument("-s", "--scriptName", type=str, required=True,
                        help="提交脚本路径")
    parser.add_argument("-n", "--jobName", type=str, required=True,
                        help="任务名称")
    parser.add_argument("-p", "--partition", type=str, required=True,
                        help="LSF 队列，例如 fatA")
    parser.add_argument("-c", "--cpuNumber", type=int, required=True,
                        help="CPU 数量")
    parser.add_argument("-m", "--machine", nargs="?", const="", default=None,
                        help="指定节点；-m 后留空时按节点组选择 NJOBS 最少的 ok 节点")
    parser.add_argument("-y", "--yes", action="store_true", help="直接提交，不询问")
    return parser.parse_args(argv)


def parse_interact(argv):
    parser = argparse.ArgumentParser(
        prog="bsub_v4.py interact",
        description=(
            "Start a batch-backed command console. Each entered command is "
            "submitted as a separate LSF batch job; stdout and stderr are "
            "displayed when the job finishes. The console remains open for "
            "subsequent commands."
        ),
        epilog=(
            "Examples:\n"
            "  bsub_v4.py interact -c 1 -n dev -p fatA -l 'hostname'\n"
            "  bsub_v4.py interact -c 1 -n dev -p fatA -e esmfold -l 'python --version'\n"
            "\n"
            "The -e option is optional and defaults to an empty value. "
            "When specified, the Conda environment is activated separately "
            "for every submitted command. This mode is not a persistent "
            "remote shell and does not provide a TTY/PTY."
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("-c", "--cpuNumber", type=int, required=True,
                        help="CPU cores requested for each command job")
    parser.add_argument("-n", "--jobName", type=str, required=True,
                        help="Job-name prefix")
    parser.add_argument("-p", "--partition", type=str, required=True,
                        help="LSF queue name")
    parser.add_argument("-e", "--environment", "--enviroment", default="",
                        help="Optional Conda environment name or path (default: empty)")
    parser.add_argument("-l", "--line", required=True,
                        help="First command to execute after starting the console; quote the command")
    return parser.parse_args(argv)


def main():
    argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help"):
        print_main_help()
        return 0
    try:
        if argv and argv[0] == "interact":
            args = parse_interact(argv[1:])
            validate_common(args.jobName, args.partition, args.cpuNumber)
            InteractConsole(args.cpuNumber, args.jobName, args.partition,
                            args.environment, args.line).run()
        else:
            args = parse_legacy(argv)
            submit_legacy(args)
    except (ValueError, RuntimeError, OSError) as exc:
        print("错误: %s" % exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

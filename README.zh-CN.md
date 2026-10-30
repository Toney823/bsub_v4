# bsub_v4

[English](README.md) | **简体中文**

一个面向 IBM Spectrum LSF 的轻量命令行封装：既可以提交现成的 Shell 脚本，也可以
开启一个「批处理控制台」，把每条命令作为独立作业提交并回显输出。

## 功能

- **脚本提交模式**：兼容早期 `bsub_v3.py` 的用法，读取脚本并生成带 `#BSUB` 指令的提交文件。
- **`interact` 命令控制台**：逐行输入命令，每条命令独立提交为 LSF 作业，作业结束后回显 stdout/stderr。
- **节点选择**：`-m` 留空时，根据 `-p` 从 `bmgroup -w` 匹配节点组，并用 `bhosts -w` 挑选状态为 `ok`、`NJOBS` 最少的节点。
- **Conda 环境**：每个作业结束时会记录计算节点上的当前环境，后续作业启动时自动恢复；`-e` 可指定初始环境。
- **受控的环境切换**：会话内最多允许一次 `activate` 切换到命名环境、一次 `deactivate` 返回 base，且必须单独成行输入（不能写成 `conda activate x && python y`）。
- **日志归档**：每会话的作业脚本与日志保存在 `~/.interface/jobs/<session>/`。

## 依赖

- Python 3.6+
- Linux 登录节点，PATH 中可用的 LSF 命令：`bsub`、`bjobs`、`bhosts`、`bmgroup`
- 交互控制台使用 `readline`/`termios`（缺失时会自动降级）

## 用法

```bash
python3 bsub_v4.py -h
```

### 1. 提交脚本

```bash
python3 bsub_v4.py -s run.sh -n test -p fatA -c 4 -y
```

- `-s` 脚本路径，`-n` 任务名，`-p` LSF 队列，`-c` CPU 数
- `-m` 可选：指定节点；`-m` 后留空则自动挑选 NJOBS 最少的 ok 节点
- `-y` 跳过确认直接提交

### 2. 批处理控制台

```bash
python3 bsub_v4.py interact -c 1 -n dev -p fatA -l 'hostname'
python3 bsub_v4.py interact -c 1 -n dev -p fatA -e esmfold -l 'python --version'
```

- `-l` 是进入控制台后执行的第一条命令
- `-e` 可选，指定初始 Conda 环境（默认不激活）
- 输入 `exit` / `quit` 或 Ctrl-D 退出

## 注意

`interact` 不是持久远程 Shell，也不提供 TTY/PTY：

- 每条命令都是一个独立批处理作业，普通 shell 变量和 `export` 不会跨作业保留；
- `cd` 只在登录节点维护会话工作目录，不改变计算节点上的路径；
- 需要跨作业保留的状态请写进 `~/.interface/init.sh`（每个作业都会 source 它）。

## 许可

本项目采用 [MIT License](LICENSE)。

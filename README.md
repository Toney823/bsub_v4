# bsub_v4

**English** | [简体中文](README.zh-CN.md)

A lightweight command-line frontend for IBM Spectrum LSF. It supports the classic
script-submission interface and a batch-backed command console in which every
entered command is submitted as its own LSF job.

## Features

- **Script submission mode**: compatible with the earlier `bsub_v3.py`; reads a script and generates a submission file with `#BSUB` directives.
- **`interact` console**: type one command per line; each command is submitted as an independent LSF job, and its stdout/stderr are shown when the job finishes.
- **Node selection**: when `-m` is given without a host, the script matches a host group from `bmgroup -w` for the `-p` queue and picks the `ok` host with the fewest `NJOBS` from `bhosts -w`.
- **Conda environments**: after each job the active environment on the compute node is recorded and restored before the next job; `-e` sets the initial environment.
- **Controlled environment switching**: at most one `activate` to a named environment and one `deactivate` back to base per session, each entered on its own line (not `conda activate x && python y`).
- **Log archiving**: per-session job scripts and logs are stored in `~/.interface/jobs/<session>/`.

## Requirements

- Python 3.6+
- A Linux login node with LSF commands on `PATH`: `bsub`, `bjobs`, `bhosts`, `bmgroup`
- The console uses `readline`/`termios` and degrades gracefully when they are missing

## Usage

```bash
python3 bsub_v4.py -h
```

### 1. Submit a script

```bash
python3 bsub_v4.py -s run.sh -n test -p fatA -c 4 -y
```

- `-s` script path, `-n` job name, `-p` LSF queue, `-c` CPU count
- `-m` optional: a specific host; an empty `-m` picks the `ok` host with the fewest `NJOBS`
- `-y` skip the confirmation prompt

### 2. Batch-backed console

```bash
python3 bsub_v4.py interact -c 1 -n dev -p fatA -l 'hostname'
python3 bsub_v4.py interact -c 1 -n dev -p fatA -e esmfold -l 'python --version'
```

- `-l` is the first command executed after entering the console
- `-e` optional initial Conda environment (default: none)
- Enter `exit` / `quit` or press Ctrl-D to leave

## Notes

`interact` is not a persistent remote shell and provides no TTY/PTY:

- Each command is an independent batch job; ordinary shell variables and `export`s are not carried across jobs.
- `cd` only maintains the session working directory on the login node, not on compute nodes.
- Keep state that must survive across jobs in `~/.interface/init.sh`, which every job sources.

## License

Released under the [MIT License](LICENSE).

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
开机自启动设置 —— 可视化配置「开机自动运行哪个自瞄程序」。

用法：
    python3 tools/autostart_gui.py              # 打开图形界面
    python3 tools/autostart_gui.py status       # 命令行查看状态
    python3 tools/autostart_gui.py enable awake1.0
    python3 tools/autostart_gui.py disable

原理（照抄 TJ2.0/TJ2.1 的 autostart.sh 思路，但改成用户级，不用 sudo）：
    1. 生成一个启动包装脚本 ~/.config/awake-autostart/start.sh
       - 先 sleep（等 USB 设备就绪，默认 8 秒）
       - 用 flock 加锁，保证只跑一个实例（相机/串口都是独占的）
       - 用 setsid 起一个进程组，方便一键停掉整棵进程树
       - 输出重定向到 ~/.config/awake-autostart/logs/start-<日期>.log
    2. 写一个自启动入口，二选一：
       desktop：~/.config/autostart/awake-autostart.desktop  （登录桌面后启动）
       systemd：~/.config/systemd/user/awake-autostart.service（开机即启动，不需登录）

为什么不改 TJ2.1 那种 systemd **系统级** 服务：
    那需要 sudo（root 才能写 /etc/systemd/system），而用户级 + linger 已经能做到
    "开机就起"，不需要密码。
"""

from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

HOME = Path.home()
CONF_DIR = HOME / ".config" / "awake-autostart"
CONF_FILE = CONF_DIR / "config.json"
WRAPPER = CONF_DIR / "start.sh"
LOG_DIR = CONF_DIR / "logs"
PID_FILE = CONF_DIR / "child.pid"
LOCK_FILE = CONF_DIR / "run.lock"
XDG_AUTOSTART = HOME / ".config" / "autostart" / "awake-autostart.desktop"
SYSTEMD_UNIT = HOME / ".config" / "systemd" / "user" / "awake-autostart.service"

# ---------------------------------------------------------------------------
# 可选程序表：key -> (显示名, 工作目录, 命令, 备注)
# 路径按实际存在的项目自动标记可用性
# ---------------------------------------------------------------------------
PROGRAMS = [
    {
        "key": "awake1.0",
        "name": "awake1.0 自瞄",
        "workdir": HOME / "下载" / "awake1.0",
        "cmd": "./run_autoaim.sh",
        "desc": "本项目（迈德威视相机 + 串口 IMU + 装甲板识别）",
        "env": {},
    },
    {
        "key": "tj2.1",
        "name": "TJ2.1 自瞄",
        "workdir": HOME / "下载" / "TJ2.1",
        "cmd": "./autostart.sh",
        "desc": "TJ2.1（screen 里跑 auto_aim_test）",
        # TJ2.1/autostart.sh 里有个 TJ2_AUTOSTART_DISABLED 守卫，默认 1 会直接退出；
        # 该脚本已改成读环境变量，这里传 0 让它真正启动。
        "env": {"TJ2_AUTOSTART_DISABLED": "0"},
    },
    {
        "key": "custom",
        "name": "自定义命令",
        "workdir": HOME,
        "cmd": "",
        "desc": "自己填命令（在工作目录下执行）",
        "env": {},
    },
]

DEFAULT_CONF = {
    "enabled": False,
    "target": "awake1.0",
    "custom_cmd": "",
    "custom_workdir": str(HOME),
    "mode": "desktop",   # desktop | systemd
    "delay": 8,          # 启动前等待秒数（等 USB 相机/串口就绪）
}


# ---------------------------------------------------------------------------
# 配置读写
# ---------------------------------------------------------------------------
def load_conf() -> dict:
    conf = dict(DEFAULT_CONF)
    try:
        if CONF_FILE.is_file():
            conf.update(json.loads(CONF_FILE.read_text(encoding="utf-8")))
    except Exception:
        pass
    return conf


def save_conf(conf: dict) -> None:
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    CONF_FILE.write_text(json.dumps(conf, ensure_ascii=False, indent=2), encoding="utf-8")


def find_program(key: str) -> dict:
    for p in PROGRAMS:
        if p["key"] == key:
            return p
    return PROGRAMS[0]


def program_available(p: dict) -> tuple[bool, str]:
    """检查程序是否就位，返回 (可用, 原因)"""
    if p["key"] == "custom":
        return True, ""
    wd = Path(p["workdir"])
    if not wd.is_dir():
        return False, f"目录不存在: {wd}"
    target = wd / p["cmd"].lstrip("./")
    if not target.exists():
        return False, f"启动脚本不存在: {target}"
    return True, ""


# ---------------------------------------------------------------------------
# 生成包装脚本 / 自启动入口
# ---------------------------------------------------------------------------
def resolve_command(conf: dict) -> tuple[Path, str, dict]:
    """返回 (工作目录, 命令, 额外环境变量)"""
    p = find_program(conf.get("target", "awake1.0"))
    if p["key"] == "custom":
        return Path(conf.get("custom_workdir") or HOME), conf.get("custom_cmd", ""), {}
    return Path(p["workdir"]), p["cmd"], dict(p["env"])


def write_wrapper(conf: dict) -> None:
    workdir, cmd, env = resolve_command(conf)
    delay = int(conf.get("delay", 8) or 0)
    env_lines = "\n".join(f'export {k}="{v}"' for k, v in env.items())

    script = f"""#!/bin/bash
# 由「开机自启动设置」自动生成，请勿手改（改配置请用图形界面）
# 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
# 目标程序: {conf.get('target')}
set -u

CONF_DIR="{CONF_DIR}"
LOG_DIR="$CONF_DIR/logs"
LOCK="$CONF_DIR/run.lock"
PIDFILE="$CONF_DIR/child.pid"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/start-$(date '+%Y-%m-%d').log"

echo "[$(date '+%F %T')] ===== 自启动开始 (目标: {conf.get('target')}) =====" >> "$LOG"

# 只允许一个实例：相机和串口都是独占设备，跑两个会互相抢
exec 9>"$LOCK"
if ! flock -n 9; then
    echo "[$(date '+%F %T')] 已有实例在运行，本次跳过" >> "$LOG"
    exit 0
fi

# 等 USB 相机/串口就绪（照抄 TJ2.1 autostart.sh 的做法）
echo "[$(date '+%F %T')] 等待 {delay} 秒让设备就绪..." >> "$LOG"
sleep {delay}

cd "{workdir}" || {{ echo "[$(date '+%F %T')] 工作目录不存在: {workdir}" >> "$LOG"; exit 1; }}
{env_lines}

echo "[$(date '+%F %T')] 启动: {cmd}  (cwd=$(pwd))" >> "$LOG"
# setsid: 独立进程组，方便「停止」时整棵树一起杀
setsid {cmd} >> "$LOG" 2>&1 &
CHILD=$!
echo "$CHILD" > "$PIDFILE"
echo "[$(date '+%F %T')] 子进程 PID=$CHILD" >> "$LOG"

wait "$CHILD"
RC=$?
echo "[$(date '+%F %T')] 进程退出，返回码 $RC" >> "$LOG"
rm -f "$PIDFILE"
exit $RC
"""
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    WRAPPER.write_text(script, encoding="utf-8")
    WRAPPER.chmod(0o755)


def write_desktop_entry(conf: dict) -> None:
    p = find_program(conf.get("target", "awake1.0"))
    XDG_AUTOSTART.parent.mkdir(parents=True, exist_ok=True)
    content = f"""[Desktop Entry]
Type=Application
Name=awake 开机自启动
Comment=自动启动: {p['name']}
Exec=/bin/bash {WRAPPER}
Terminal=false
NoDisplay=true
Hidden=false
X-GNOME-Autostart-enabled=true
X-GNOME-Autostart-Delay=0
"""
    XDG_AUTOSTART.write_text(content, encoding="utf-8")


def write_systemd_unit(conf: dict) -> None:
    SYSTEMD_UNIT.parent.mkdir(parents=True, exist_ok=True)
    content = f"""[Unit]
Description=awake 开机自启动 ({conf.get('target')})
After=network.target

[Service]
Type=simple
ExecStart=/bin/bash {WRAPPER}
Restart=no
TimeoutStopSec=10

[Install]
WantedBy=default.target
"""
    SYSTEMD_UNIT.write_text(content, encoding="utf-8")


def run_quiet(args: list[str], timeout: int = 15) -> tuple[int, str]:
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except Exception as e:
        return 1, str(e)


def systemd_user_bus_ok() -> bool:
    rc, _ = run_quiet(["systemctl", "--user", "show-environment"], timeout=8)
    return rc == 0


# ---------------------------------------------------------------------------
# 启用 / 停用
# ---------------------------------------------------------------------------
def apply(conf: dict) -> str:
    """按配置落盘，返回一句人话说明结果"""
    save_conf(conf)
    if not conf.get("enabled"):
        return do_disable()
    return do_enable(conf)


def do_enable(conf: dict) -> str:
    p = find_program(conf.get("target", "awake1.0"))
    ok, why = program_available(p)
    if not ok:
        return f"未启用：{p['name']} 不可用 —— {why}"

    write_wrapper(conf)
    mode = conf.get("mode", "desktop")

    if mode == "systemd":
        write_systemd_unit(conf)
        if XDG_AUTOSTART.is_file():
            XDG_AUTOSTART.unlink()
        if not systemd_user_bus_ok():
            return "已写配置，但 systemctl --user 不可用（请在桌面会话里重试，或改用「登录桌面后启动」）"
        run_quiet(["systemctl", "--user", "daemon-reload"])
        rc, out = run_quiet(["systemctl", "--user", "enable", "--now", "awake-autostart.service"])
        # linger：不登录桌面也能开机启动
        run_quiet(["loginctl", "enable-linger", os.environ.get("USER", "noto")])
        if rc != 0:
            return f"启用失败：{out}"
        return f"已启用（开机即启动 / systemd 用户服务）：{p['name']}"

    # desktop 模式
    write_desktop_entry(conf)
    if SYSTEMD_UNIT.is_file():
        run_quiet(["systemctl", "--user", "disable", "awake-autostart.service"])
        SYSTEMD_UNIT.unlink(missing_ok=True)
        run_quiet(["systemctl", "--user", "daemon-reload"])
    return f"已启用（登录桌面后启动）：{p['name']}"


def do_disable() -> str:
    removed = []
    if XDG_AUTOSTART.is_file():
        XDG_AUTOSTART.unlink()
        removed.append("桌面自启动项")
    if SYSTEMD_UNIT.is_file():
        run_quiet(["systemctl", "--user", "disable", "awake-autostart.service"])
        SYSTEMD_UNIT.unlink(missing_ok=True)
        run_quiet(["systemctl", "--user", "daemon-reload"])
        removed.append("systemd 用户服务")
    return "已停用开机自启动" + (f"（移除了 {'、'.join(removed)}）" if removed else "（本来就没启用）")


def status_text() -> str:
    conf = load_conf()
    p = find_program(conf.get("target", "awake1.0"))
    if conf.get("enabled"):
        how = "开机即启动(systemd)" if conf.get("mode") == "systemd" else "登录桌面后启动"
        s = f"自启动：已启用 —— {p['name']}（{how}，延迟 {conf.get('delay', 8)} 秒）"
    else:
        s = "自启动：已停用"
    pid = read_running_pid()
    s += f"\n运行状态：{'正在运行 PID ' + str(pid) if pid else '未运行'}"
    return s


# ---------------------------------------------------------------------------
# 手动启停（测试用）
# ---------------------------------------------------------------------------
def read_running_pid() -> int | None:
    try:
        if PID_FILE.is_file():
            pid = int(PID_FILE.read_text().strip())
            os.kill(pid, 0)  # 存在性检查，无信号发出
            return pid
    except Exception:
        pass
    return None


def start_now(conf: dict) -> str:
    save_conf(conf)
    write_wrapper(conf)
    pid = read_running_pid()
    if pid:
        return f"已经在运行（PID {pid}），先停掉再启动"
    subprocess.Popen(
        ["/bin/bash", str(WRAPPER)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        cwd=str(Path.home()),
    )
    return f"已启动（延迟 {conf.get('delay', 8)} 秒后真正跑起来），看日志确认"


def stop_now() -> str:
    """停掉由本工具启动的进程（整棵进程树）。

    安全设计（踩过坑）：绝不按"负数 pid"盲发信号 —— 那会波及到当前 shell 所在的
    进程组。这里先通过 /proc 精确找出属于目标进程组的进程，逐个显式 kill，并且
    无条件跳过：
      * 自己（os.getpid()）
      * pid 1
      * 任何与调用者同组的进程
    """
    pid = read_running_pid()
    if not pid:
        return "没有在运行（找不到有效的 pid 文件）"

    try:
        target_pgid = os.getpgid(pid)
    except Exception:
        try:
            PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass
        return f"进程 {pid} 已经不存在，已清理 pid 文件"

    my_pid = os.getpid()
    my_pgid = os.getpgid(0)
    if target_pgid == my_pgid:
        return f"拒绝操作：目标进程组 {target_pgid} 与当前会话同组，为安全起见不发送信号"

    victims: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        p = int(entry.name)
        if p in (0, 1, my_pid):
            continue
        try:
            if os.getpgid(p) != target_pgid:
                continue
        except Exception:
            continue
        victims.append(p)

    killed: list[int] = []
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for p in victims:
            try:
                os.kill(p, sig)
                killed.append(p)
            except Exception:
                pass
        # 给 SIGTERM 一点时间生效
        if sig == signal.SIGTERM:
            for _ in range(20):
                time.sleep(0.1)
                alive = False
                for p in victims:
                    try:
                        os.kill(p, 0)
                        alive = True
                        break
                    except Exception:
                        pass
                if not alive:
                    break

    try:
        PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass

    # 等锁释放
    time.sleep(0.2)
    n = len(set(killed))
    return f"已停止 {n} 个进程（进程组 {target_pgid}）" if n else "没有找到可停止的进程"


def latest_log() -> Path | None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logs = sorted(LOG_DIR.glob("start-*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    return logs[0] if logs else None


def tail(path: Path, n: int = 60) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except Exception as e:
        return f"读取失败: {e}"


# ---------------------------------------------------------------------------
# 图形界面
# ---------------------------------------------------------------------------
def run_gui() -> int:
    import tkinter as tk
    from tkinter import messagebox, ttk

    conf = load_conf()
    root = tk.Tk()
    root.title("开机自启动设置")
    root.geometry("680x560")
    root.minsize(620, 520)
    try:
        root.tk.call("tk", "scaling", 1.25)
    except Exception:
        pass

    state = {"target": conf.get("target", "awake1.0"), "mode": conf.get("mode", "desktop")}
    custom_cmd = tk.StringVar(value=conf.get("custom_cmd", ""))
    custom_wd = tk.StringVar(value=conf.get("custom_workdir", str(HOME)))
    delay_var = tk.IntVar(value=int(conf.get("delay", 8) or 0))

    # ---------- 顶部状态 ----------
    top = ttk.Frame(root, padding=(14, 12, 14, 6))
    top.pack(fill="x")
    status_lbl = ttk.Label(top, text="", font=("", 11, "bold"))
    status_lbl.pack(anchor="w")
    run_lbl = ttk.Label(top, text="", foreground="#666")
    run_lbl.pack(anchor="w", pady=(4, 0))

    def refresh_status():
        c = load_conf()
        p = find_program(c.get("target", "awake1.0"))
        if c.get("enabled"):
            how = "开机即启动（systemd）" if c.get("mode") == "systemd" else "登录桌面后启动"
            status_lbl.config(text=f"● 自启动已启用  ——  {p['name']}  [{how}]", foreground="#0a7d28")
        else:
            status_lbl.config(text="○ 自启动已停用", foreground="#a33")
        pid = read_running_pid()
        run_lbl.config(
            text=f"程序状态：正在运行（PID {pid}）" if pid else "程序状态：未运行"
        )

    # ---------- 程序选择 ----------
    box = ttk.LabelFrame(root, text=" 1. 选择要自启动的程序 ", padding=(12, 8))
    box.pack(fill="x", padx=14, pady=6)
    choice = tk.StringVar(value=state["target"])
    for p in PROGRAMS:
        ok, why = program_available(p)
        row = ttk.Frame(box)
        row.pack(fill="x", pady=2)
        rb = ttk.Radiobutton(
            row, text=p["name"], value=p["key"], variable=choice,
            command=lambda: on_pick(),
        )
        rb.pack(side="left")
        note = p["desc"] if ok else f"{p['desc']}  ⚠ {why}"
        ttk.Label(row, text=f"   {note}", foreground=("#666" if ok else "#b00")).pack(side="left")
        if not ok:
            rb.state(["disabled"])

    cust = ttk.Frame(box)
    cust.pack(fill="x", pady=(4, 0))
    ttk.Label(cust, text="自定义命令：").pack(side="left")
    ent_cmd = ttk.Entry(cust, textvariable=custom_cmd)
    ent_cmd.pack(side="left", fill="x", expand=True)
    cust2 = ttk.Frame(box)
    cust2.pack(fill="x", pady=(4, 0))
    ttk.Label(cust2, text="工作目录：").pack(side="left")
    ent_wd = ttk.Entry(cust2, textvariable=custom_wd)
    ent_wd.pack(side="left", fill="x", expand=True)

    def on_pick():
        is_custom = choice.get() == "custom"
        for w in (ent_cmd, ent_wd):
            w.state(["!disabled"] if is_custom else ["disabled"])

    # ---------- 启动方式 ----------
    box2 = ttk.LabelFrame(root, text=" 2. 什么时候启动 ", padding=(12, 8))
    box2.pack(fill="x", padx=14, pady=6)
    mode_var = tk.StringVar(value=state["mode"])
    ttk.Radiobutton(
        box2, text="登录桌面后启动（推荐，最稳，不需要密码）",
        value="desktop", variable=mode_var,
    ).pack(anchor="w")
    ttk.Radiobutton(
        box2, text="开机即启动，不用登录（systemd 用户服务 + linger）",
        value="systemd", variable=mode_var,
    ).pack(anchor="w")
    delay_row = ttk.Frame(box2)
    delay_row.pack(fill="x", pady=(6, 0))
    ttk.Label(delay_row, text="启动前等待：").pack(side="left")
    ttk.Spinbox(delay_row, from_=0, to=60, textvariable=delay_var, width=5).pack(side="left")
    ttk.Label(delay_row, text=" 秒（等 USB 相机/串口就绪，TJ2 用的是 5~8 秒）").pack(side="left")

    # ---------- 按钮 ----------
    btns = ttk.Frame(root, padding=(14, 4))
    btns.pack(fill="x")

    def collect() -> dict:
        c = load_conf()
        c.update(
            target=choice.get(),
            custom_cmd=custom_cmd.get().strip(),
            custom_workdir=custom_wd.get().strip(),
            mode=mode_var.get(),
            delay=int(delay_var.get() or 0),
        )
        return c

    def do_enable():
        c = collect()
        if c["target"] == "custom" and not c["custom_cmd"]:
            messagebox.showwarning("缺少命令", "选择了「自定义命令」但命令是空的")
            return
        c["enabled"] = True
        msg = apply(c)
        refresh_status()
        log = latest_log()
        extra = f"\n\n日志：{log}" if log else ""
        messagebox.showinfo("结果", msg + extra)
        on_pick()

    def do_disable():
        c = collect()
        c["enabled"] = False
        msg = apply(c)
        refresh_status()
        messagebox.showinfo("结果", msg)

    def do_start():
        msg = start_now(collect())
        refresh_status()
        messagebox.showinfo("结果", msg)

    def do_stop():
        msg = stop_now()
        refresh_status()
        messagebox.showinfo("结果", msg)

    def do_log():
        log = latest_log()
        if not log:
            messagebox.showinfo("日志", "还没有日志文件（程序还没通过本工具启动过）")
            return
        win = tk.Toplevel(root)
        win.title(f"运行日志 - {log.name}")
        win.geometry("860x560")
        txt = tk.Text(win, wrap="none")
        txt.pack(fill="both", expand=True)
        txt.insert("1.0", tail(log, 400))
        txt.see("end")
        txt.config(state="disabled")
        ttk.Label(win, text=f"文件：{log}").pack(anchor="w")

    ttk.Button(btns, text="启用并保存", command=do_enable).pack(side="left", padx=(0, 6))
    ttk.Button(btns, text="停用自启动", command=do_disable).pack(side="left", padx=6)
    ttk.Button(btns, text="立即启动（测试）", command=do_start).pack(side="left", padx=6)
    ttk.Button(btns, text="停止", command=do_stop).pack(side="left", padx=6)
    ttk.Button(btns, text="查看日志", command=do_log).pack(side="left", padx=6)
    ttk.Button(btns, text="刷新", command=refresh_status).pack(side="right")

    hint = ttk.Label(
        root,
        text=(
            "说明：配置存放在 ~/.config/awake-autostart/ ，自启动入口在 ~/.config/autostart/ 。\n"
            "相机和串口都是独占设备，awake1.0 和 TJ2.1 不能同时自启动。"
        ),
        foreground="#666", justify="left",
    )
    hint.pack(anchor="w", padx=16, pady=(30, 10))

    on_pick()
    refresh_status()
    root.mainloop()
    return 0


# ---------------------------------------------------------------------------
def main() -> int:
    args = sys.argv[1:]
    if not args:
        return run_gui()

    cmd = args[0]
    if cmd == "status":
        print(status_text())
        return 0
    if cmd == "enable":
        conf = load_conf()
        if len(args) > 1:
            conf["target"] = args[1]
        if len(args) > 2:
            conf["mode"] = args[2]
        conf["enabled"] = True
        print(apply(conf))
        return 0
    if cmd == "disable":
        conf = load_conf()
        conf["enabled"] = False
        print(apply(conf))
        return 0
    if cmd == "start":
        print(start_now(load_conf()))
        return 0
    if cmd == "stop":
        print(stop_now())
        return 0
    if cmd == "log":
        log = latest_log()
        print(tail(log, 60) if log else "还没有日志")
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
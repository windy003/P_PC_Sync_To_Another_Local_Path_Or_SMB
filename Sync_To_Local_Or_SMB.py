"""
文件/文件夹同步工具（基于 watchdog）。
从 .env 读取多组同步对，监听源目录的变化，
将所有变更（创建/修改/删除/移动）实时镜像到目标目录。

每个同步对支持:
  - DEPTH: 限制同步的目录层数（0 = 无限制）
  - IGNORE: 独立的忽略规则（与全局 SYNC_IGNORE 合并生效）
"""

import os
import re
import sys
import time
import shutil
import fnmatch
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# 从脚本所在目录加载 .env 配置（不使用 dotenv 的转义，避免反斜杠路径被误解析）
def _load_env_raw(env_path: Path):
    """原样加载 .env，不对反斜杠做转义处理。"""
    if not env_path.exists():
        return
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            # 去掉值两端的引号（单引号或双引号），但不做转义
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            os.environ[key] = value

_load_env_raw(Path(__file__).parent / ".env")


# --------------- 配置 ---------------

@dataclass
class SyncPair:
    source: str
    dest: str
    depth: int = 0  # 0 = 无限制
    ignore: list[str] = field(default_factory=list)


def load_sync_pairs() -> list[SyncPair]:
    """从环境变量解析 SYNC_PAIR_<N>_SOURCE/DEST/DEPTH/IGNORE。"""
    global_ignore = _parse_patterns(os.getenv("SYNC_IGNORE", ""))

    raw: dict[str, dict[str, str]] = {}
    for key, value in os.environ.items():
        m = re.match(r"SYNC_PAIR_(\d+)_(SOURCE|DEST|DEPTH|IGNORE)$", key)
        if m:
            idx, role = m.group(1), m.group(2).lower()
            raw.setdefault(idx, {})[role] = value.strip()

    pairs = []
    for idx in sorted(raw, key=int):
        p = raw[idx]
        src, dst = p.get("source"), p.get("dest")
        if not src or not dst:
            logging.warning("同步对 %s 配置不完整，已跳过", idx)
            continue

        depth = int(p.get("depth", "0"))
        pair_ignore = _parse_patterns(p.get("ignore", ""))
        merged_ignore = list(set(global_ignore + pair_ignore))

        pairs.append(SyncPair(
            source=os.path.normpath(src),
            dest=os.path.normpath(dst),
            depth=depth,
            ignore=merged_ignore,
        ))
    return pairs


def _parse_patterns(raw: str) -> list[str]:
    """解析逗号分隔的忽略规则字符串。"""
    return [p.strip() for p in raw.split(",") if p.strip()]


# --------------- 工具函数 ---------------

def should_ignore(path: str, patterns: list[str]) -> bool:
    """判断路径是否匹配忽略规则。"""
    name = os.path.basename(path)
    return any(fnmatch.fnmatch(name, pat) for pat in patterns)


def get_depth(path: str, base: str) -> int:
    """计算 path 相对于 base 的目录层级深度，base 本身为 0。"""
    rel = os.path.relpath(path, base)
    if rel == ".":
        return 0
    return len(Path(rel).parts)


def exceeds_depth(path: str, base: str, max_depth: int) -> bool:
    """检查路径是否超过允许的最大深度。max_depth=0 表示无限制。"""
    if max_depth == 0:
        return False
    return get_depth(path, base) >= max_depth


def full_sync(pair: SyncPair):
    """一次性全量镜像同步: 源目录 -> 目标目录（复制新增/更新文件，删除多余文件）。"""
    src, dst = pair.source, pair.dest

    # 复制 / 更新
    for root, dirs, files in os.walk(src):
        # 过滤掉需要忽略的目录
        dirs[:] = [d for d in dirs if not should_ignore(d, pair.ignore)]

        # 深度检查：如果再深入就超过限制，不再继续深入
        current_depth = get_depth(root, src)
        if pair.depth > 0 and current_depth + 1 >= pair.depth:
            dirs.clear()

        # 如果当前目录本身已超过允许深度，跳过
        if exceeds_depth(root, src, pair.depth):
            continue

        rel = os.path.relpath(root, src)
        dst_root = os.path.join(dst, rel)
        os.makedirs(dst_root, exist_ok=True)

        for f in files:
            if should_ignore(f, pair.ignore):
                continue
            s = os.path.join(root, f)
            d = os.path.join(dst_root, f)
            if not os.path.exists(d) or os.stat(s).st_mtime > os.stat(d).st_mtime:
                shutil.copy2(s, d)
                logging.debug("已复制: %s -> %s", s, d)

    # 删除目标目录中源目录已不存在的文件/文件夹，或超出深度限制的内容
    for root, dirs, files in os.walk(dst, topdown=False):
        rel = os.path.relpath(root, dst)
        src_root = os.path.join(src, rel)

        # 如果目标目录中此路径超出深度限制，整个删除
        if exceeds_depth(root, dst, pair.depth):
            shutil.rmtree(root, ignore_errors=True)
            logging.debug("已删除（超出深度）: %s", root)
            continue

        for f in files:
            src_file = os.path.join(src_root, f)
            if not os.path.exists(src_file):
                target = os.path.join(root, f)
                os.remove(target)
                logging.debug("已删除文件: %s", target)

        for d in dirs:
            src_dir = os.path.join(src_root, d)
            if not os.path.exists(src_dir):
                target = os.path.join(root, d)
                shutil.rmtree(target, ignore_errors=True)
                logging.debug("已删除目录: %s", target)


# --------------- 事件处理器 ---------------

class SyncHandler(FileSystemEventHandler):
    def __init__(self, pair: SyncPair):
        self.src = pair.source
        self.dst = pair.dest
        self.depth = pair.depth
        self.patterns = pair.ignore

    def _dst_path(self, src_path: str) -> str:
        """将源路径转换为对应的目标路径。"""
        rel = os.path.relpath(src_path, self.src)
        return os.path.join(self.dst, rel)

    def _should_skip(self, path: str, is_directory: bool = False) -> bool:
        """判断是否应该跳过此路径（匹配忽略规则或超出深度）。"""
        if should_ignore(path, self.patterns):
            return True
        if self.depth > 0:
            depth = get_depth(path, self.src)
            # 目录：depth >= max_depth 时跳过（与 full_sync 一致）
            # 文件：depth > max_depth 时跳过（文件比所在目录深一级）
            if is_directory and depth >= self.depth:
                return True
            if not is_directory and depth > self.depth:
                return True
        return False

    # --- 事件回调 ---

    def on_created(self, event):
        """文件或目录被创建时触发。"""
        if self._should_skip(event.src_path, event.is_directory):
            return
        dst = self._dst_path(event.src_path)
        try:
            if event.is_directory:
                os.makedirs(dst, exist_ok=True)
                logging.info("[创建目录] %s -> %s", event.src_path, dst)
            else:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(event.src_path, dst)
                logging.info("[创建文件] %s -> %s", event.src_path, dst)
        except Exception as e:
            logging.error("创建同步失败: %s", e)

    def on_modified(self, event):
        """文件被修改时触发。"""
        if event.is_directory or self._should_skip(event.src_path, False):
            return
        dst = self._dst_path(event.src_path)
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(event.src_path, dst)
            logging.info("[修改文件] %s -> %s", event.src_path, dst)
        except Exception as e:
            logging.error("修改同步失败: %s", e)

    def on_deleted(self, event):
        """文件或目录被删除时触发。"""
        if self._should_skip(event.src_path, event.is_directory):
            return
        dst = self._dst_path(event.src_path)
        try:
            if event.is_directory:
                shutil.rmtree(dst, ignore_errors=True)
                logging.info("[删除目录] %s", dst)
            else:
                if os.path.exists(dst):
                    os.remove(dst)
                    logging.info("[删除文件] %s", dst)
        except Exception as e:
            logging.error("删除同步失败: %s", e)

    def on_moved(self, event):
        """文件或目录被移动/重命名时触发。"""
        if self._should_skip(event.src_path, event.is_directory) and self._should_skip(event.dest_path, event.is_directory):
            return
        src_dst = self._dst_path(event.src_path)
        dest_dst = self._dst_path(event.dest_path)

        # 如果移动目标超出深度限制，仅执行删除操作
        if exceeds_depth(event.dest_path, self.src, self.depth):
            if os.path.exists(src_dst):
                if os.path.isdir(src_dst):
                    shutil.rmtree(src_dst, ignore_errors=True)
                else:
                    os.remove(src_dst)
                logging.info("[移出范围] 已删除 %s（目标超出深度限制）", src_dst)
            return

        try:
            if os.path.exists(src_dst):
                os.makedirs(os.path.dirname(dest_dst), exist_ok=True)
                shutil.move(src_dst, dest_dst)
                logging.info("[移动]     %s -> %s", src_dst, dest_dst)
        except Exception as e:
            logging.error("移动同步失败: %s", e)


class SingleFileHandler(FileSystemEventHandler):
    """监听单个文件的变化，将其同步到目标目录。

    针对"被程序频繁重写"的文件（如输入法词典）做了三点加固：
      1. 处理 on_moved —— 编辑器/输入法常用"写临时文件 + 改名覆盖"保存，
         这会触发 moved 而非 modified，旧实现会漏掉。
      2. 防抖 —— 连续事件合并，延迟后再复制，避开写入中途。
      3. 大小校验 + 重试 —— 避免把写到一半的残缺/空文件复制到目标。
    """
    _DEBOUNCE_SEC = 0.4
    _RETRY = 5
    _RETRY_WAIT = 0.3

    def __init__(self, src_file: str, dst_dir: str):
        self.src_file = os.path.normpath(src_file)
        self.dst_dir = dst_dir
        self.dst_file = os.path.join(dst_dir, os.path.basename(src_file))
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def _is_target(self, path: str) -> bool:
        return os.path.normpath(path) == self.src_file

    def _schedule_copy(self):
        """防抖：连续事件合并为一次，延迟后再复制。"""
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self._DEBOUNCE_SEC, self._do_copy)
            self._timer.daemon = True
            self._timer.start()

    def _do_copy(self):
        with self._lock:
            self._timer = None
        # 源文件可能正被占用或写入中途，重试若干次并校验大小一致
        for attempt in range(1, self._RETRY + 1):
            try:
                if not os.path.exists(self.src_file):
                    return
                os.makedirs(self.dst_dir, exist_ok=True)
                shutil.copy2(self.src_file, self.dst_file)
                if os.path.getsize(self.src_file) == os.path.getsize(self.dst_file):
                    logging.info("[同步文件] %s -> %s", self.src_file, self.dst_file)
                    return
                logging.warning("文件大小不一致（可能写入中途），重试: %s", self.src_file)
            except Exception as e:
                logging.warning("同步文件失败（第 %d 次）: %s", attempt, e)
            time.sleep(self._RETRY_WAIT)
        logging.error("同步文件最终失败: %s", self.src_file)

    def _do_delete(self):
        try:
            if os.path.exists(self.dst_file):
                os.remove(self.dst_file)
                logging.info("[删除文件] %s", self.dst_file)
        except Exception as e:
            logging.error("删除同步失败: %s", e)

    def on_modified(self, event):
        if event.is_directory or not self._is_target(event.src_path):
            return
        self._schedule_copy()

    def on_created(self, event):
        if event.is_directory or not self._is_target(event.src_path):
            return
        self._schedule_copy()

    def on_moved(self, event):
        if event.is_directory:
            return
        # 原子保存：临时文件被改名覆盖到源文件路径
        if self._is_target(event.dest_path):
            self._schedule_copy()
        # 源文件被改名移走，视为删除
        elif self._is_target(event.src_path):
            self._do_delete()

    def on_deleted(self, event):
        if event.is_directory or not self._is_target(event.src_path):
            return
        self._do_delete()


# --------------- .env 热重载 ---------------

def _clear_sync_env_vars():
    """清除当前进程中所有 SYNC_PAIR_* / SYNC_IGNORE 环境变量，便于重新加载。"""
    for k in list(os.environ.keys()):
        if k.startswith("SYNC_PAIR_") or k == "SYNC_IGNORE":
            del os.environ[k]


class EnvReloadHandler(FileSystemEventHandler):
    """监听 .env 文件本身的变化，触发重载回调。"""
    def __init__(self, env_path: Path, callback):
        self.env_path = os.path.normpath(str(env_path))
        self.callback = callback

    def _is_env(self, path: str) -> bool:
        return os.path.normpath(path) == self.env_path

    def on_modified(self, event):
        if not event.is_directory and self._is_env(event.src_path):
            self.callback()

    def on_created(self, event):
        if not event.is_directory and self._is_env(event.src_path):
            self.callback()

    def on_moved(self, event):
        if event.is_directory:
            return
        # 编辑器有时通过 "写临时文件 + 重命名" 完成保存
        if self._is_env(event.src_path) or self._is_env(event.dest_path):
            self.callback()


class SyncManager:
    """管理所有同步对的生命周期，支持 .env 修改后动态增删同步对。"""

    def __init__(self, env_path: Path):
        self.env_path = env_path
        self.observer = Observer()
        # pair_key -> (handler, ObservedWatch)
        self.watches: dict[tuple, tuple] = {}
        self._reload_timer: threading.Timer | None = None
        self._lock = threading.Lock()

    @staticmethod
    def _pair_key(pair: SyncPair) -> tuple:
        """同步对的唯一标识：source/dest/depth/ignore 任一变动即视为新对。"""
        return (pair.source, pair.dest, pair.depth, tuple(sorted(pair.ignore)))

    def _initial_sync(self, pair: SyncPair) -> bool:
        if os.path.isfile(pair.source):
            os.makedirs(pair.dest, exist_ok=True)
            dst_file = os.path.join(pair.dest, os.path.basename(pair.source))
            logging.info("同步文件: %s -> %s", pair.source, dst_file)
            if not os.path.exists(dst_file) or os.stat(pair.source).st_mtime > os.stat(dst_file).st_mtime:
                shutil.copy2(pair.source, dst_file)
            return True
        if os.path.isdir(pair.source):
            os.makedirs(pair.dest, exist_ok=True)
            depth_desc = "无限制" if pair.depth == 0 else str(pair.depth)
            logging.info("同步对: %s -> %s [深度=%s, 忽略=%s]",
                         pair.source, pair.dest, depth_desc, pair.ignore)
            full_sync(pair)
            return True
        logging.error("源路径不存在: %s，已跳过", pair.source)
        return False

    def _schedule_pair(self, pair: SyncPair):
        if os.path.isfile(pair.source):
            handler = SingleFileHandler(pair.source, pair.dest)
            watch = self.observer.schedule(handler, os.path.dirname(pair.source), recursive=False)
        else:
            handler = SyncHandler(pair)
            watch = self.observer.schedule(handler, pair.source, recursive=True)
        return handler, watch

    def add_pair(self, pair: SyncPair):
        key = self._pair_key(pair)
        if key in self.watches:
            return
        if not self._initial_sync(pair):
            return
        try:
            handler, watch = self._schedule_pair(pair)
            self.watches[key] = (handler, watch)
            logging.info("[已添加] %s -> %s", pair.source, pair.dest)
        except Exception as e:
            logging.error("添加同步对失败 %s: %s", pair.source, e)

    def remove_pair(self, key: tuple):
        entry = self.watches.pop(key, None)
        if entry is None:
            return
        handler, watch = entry
        try:
            # 仅移除该 handler，不影响共享同一 watch 路径的其他 handler
            self.observer.remove_handler_for_watch(handler, watch)
            logging.info("[已移除] %s -> %s", key[0], key[1])
        except Exception as e:
            logging.error("移除同步对失败: %s", e)

    def _reload(self):
        with self._lock:
            self._reload_timer = None
            _clear_sync_env_vars()
            try:
                _load_env_raw(self.env_path)
            except Exception as e:
                logging.error("重新加载 .env 失败: %s", e)
                return

            new_pairs = load_sync_pairs()
            new_map = {self._pair_key(p): p for p in new_pairs}
            old_keys = set(self.watches.keys())
            new_keys = set(new_map.keys())

            # 同步日志级别（如果 .env 改了 SYNC_LOG_LEVEL）
            log_level = os.getenv("SYNC_LOG_LEVEL", "INFO").upper()
            logging.getLogger().setLevel(getattr(logging, log_level, logging.INFO))

            if new_keys == old_keys:
                logging.info("[.env 重载] 配置无变化")
                return

            logging.info("[.env 重载] 应用新配置...")
            for key in old_keys - new_keys:
                self.remove_pair(key)
            for key in new_keys - old_keys:
                self.add_pair(new_map[key])
            logging.info("[.env 重载完成] 当前同步对数量: %d", len(self.watches))

    def schedule_reload(self):
        """防抖：1 秒内多次 .env 修改事件合并为一次重载。"""
        with self._lock:
            if self._reload_timer is not None:
                self._reload_timer.cancel()
            self._reload_timer = threading.Timer(1.0, self._reload)
            self._reload_timer.daemon = True
            self._reload_timer.start()

    def start(self):
        pairs = load_sync_pairs()
        if not pairs:
            logging.error("未在 .env 中找到任何同步对，程序退出。")
            sys.exit(1)

        logging.info("正在执行初始全量同步...")
        for pair in pairs:
            self.add_pair(pair)

        # 监听 .env 自身（监听其所在目录，handler 内做文件名过滤）
        env_handler = EnvReloadHandler(self.env_path, self.schedule_reload)
        self.observer.schedule(env_handler, str(self.env_path.parent), recursive=False)

        self.observer.start()
        logging.info("正在监听 %d 个同步对，并监听 .env 变化，按 Ctrl+C 停止。", len(self.watches))

    def stop(self):
        with self._lock:
            if self._reload_timer is not None:
                self._reload_timer.cancel()
                self._reload_timer = None
        self.observer.stop()
        self.observer.join()


# --------------- 主程序 ---------------

def main():
    log_level = os.getenv("SYNC_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    env_path = Path(__file__).parent / ".env"
    manager = SyncManager(env_path)
    manager.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logging.info("正在停止...")
        manager.stop()


if __name__ == "__main__":
    main()

"""从会话 transcript 判断一次工具调用是否已经有了结果（tool_result 已写入）。

PermissionRequest hook 运行时，终端的确认框 / 选项框同时显示。用户在终端作答后，
hook 进程不会收到任何信号（Claude Code 2.1.287 实测），只能靠 transcript 里
出现对应的 tool_result 来发现，否则 hook 会一直等下去，吃掉之后的飞书消息。"""

import json
import os

# 首次扫描只读文件末尾这么多字节：tool_use 在 hook 启动前后才写入
TAIL_BYTES = 4 * 1024 * 1024


class ToolWatch:
    """盯住一次工具调用：transcript 里 name 和 input 都相同的 tool_use，
    在开始盯的时候还没有结果的，之后出现了 tool_result → resolved() 为真。

    开始时已经有结果的同名同参调用是历史记录，不算。读不到 transcript 时永远为假。"""

    def __init__(self, path, tool_name, tool_input):
        self.path = path if isinstance(path, str) and path else None
        self.name = tool_name
        self.input = tool_input
        self.pos = None  # 下一次从这里读；None = 还没读过
        self.buf = b""
        self.pending = set()
        self.done = set()
        self.first = True

    def _read_new_lines(self):
        try:
            with open(self.path, "rb") as f:
                if self.pos is None:
                    size = os.fstat(f.fileno()).st_size
                    self.pos = max(0, size - TAIL_BYTES)
                    skip_partial = self.pos > 0
                else:
                    skip_partial = False
                f.seek(self.pos)
                data = f.read()
        except OSError:
            return []
        self.pos += len(data)
        data = self.buf + data
        end = data.rfind(b"\n") + 1
        self.buf = data[end:]
        lines = data[:end].split(b"\n")
        if skip_partial and lines:
            lines = lines[1:]  # 从文件中间开始读，第一行不完整
        return lines

    def resolved(self) -> bool:
        if self.path is None:
            return False
        results = set()
        for line in self._read_new_lines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            msg = e.get("message") if isinstance(e, dict) else None
            content = msg.get("content") if isinstance(msg, dict) else None
            if not isinstance(content, list):
                continue
            for b in content:
                if not isinstance(b, dict):
                    continue
                if (
                    b.get("type") == "tool_use"
                    and b.get("name") == self.name
                    and b.get("input") == self.input
                ):
                    self.pending.add(b.get("id"))
                elif b.get("type") == "tool_result":
                    results.add(b.get("tool_use_id"))
        if self.first:
            # 开始盯之前就已经有结果的，是之前的同名同参调用
            self.first = False
            self.pending -= results
            self.done |= results
            return False
        self.pending -= self.done
        return bool(self.pending & results)

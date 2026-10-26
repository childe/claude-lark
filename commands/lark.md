---
description: 把当前会话桥接到飞书群（/lark [群名|chat_id|off|status]）
allowed-tools: Bash(~/.claude/lark/app/bin/claude-lark:*)
---
!`~/.claude/lark/app/bin/claude-lark slash --session ${CLAUDE_SESSION_ID} --args "$ARGUMENTS"`

如果上面的输出以 `RESULT:` 开头：把它原样转述给用户，不要做其他事。
如果上面的输出是 `NEED_NAME`：根据本会话到目前为止的内容，总结一个不超过 20 个字的中文群名，概括这个会话在做什么；然后用 Bash 执行
`~/.claude/lark/app/bin/claude-lark on --session ${CLAUDE_SESSION_ID} --name "<群名>"`，把输出转述给用户。

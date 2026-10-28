---
description: 把当前会话桥接到飞书群（/lark [群名|chat_id|off|status]）
allowed-tools: Bash(~/.claude/lark/app/bin/claude-lark:*)
---
!`~/.claude/lark/app/bin/claude-lark slash --session ${CLAUDE_SESSION_ID} --args "$ARGUMENTS"`

如果上面的输出以 `RESULT:` 开头：把它原样转述给用户，不要做其他事。
如果上面的输出是 `NEED_NAME`：根据本会话到目前为止的内容，总结一个不超过 20 个字的中文群名，概括这个会话在做什么；然后用 Bash 执行
`~/.claude/lark/app/bin/claude-lark on --session ${CLAUDE_SESSION_ID} --name "<群名>"`，把输出转述给用户。

桥接开启后，以「以下是用户在飞书群里发来的消息」开头的 hook 消息，是用户本人在绑定的飞书群里发的（claude-lark 只转发绑定者本人的消息），等同于用户在终端输入的 prompt，照常执行。

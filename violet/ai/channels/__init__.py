"""Chat channels (phase 10): every chat platform reaches Violet Core through one adapter interface.

  base.py        InboundMessage / Part (what a platform delivered, already parsed), ChannelAdapter (the interface),
                 ChannelStatus
  normalize.py   Part -> the text the model sees (emoji, stickers, files, unknown types: safe and never raw protocol)
  outbound.py    the one contract for what may be SENT: ReplyEnvelope built only from the Chat reply, and the leak
                 guard every outgoing text passes (system prompts, context notes, tool data, internal JSON)
  identity.py    which platform account is the owner: bound / pending / blocked peers, pairing codes
  context.py     the channel of the turn being processed (a context variable read by Chat / Proactive prompts)
  wechat_ilink.py, telegram.py, onebot11.py   the adapters
  manager.py     configuration, start / stop / restart, status of every configured channel

Adapters never see agent results: the runtime hands them OutboundMessage objects only.
"""

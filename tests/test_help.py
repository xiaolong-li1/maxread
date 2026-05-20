from maxread.help import group_intro_message, intro_message, plain_message_text, should_send_intro


def test_plain_message_text_handles_feishu_json():
    assert plain_message_text('{"text":"怎么用"}') == "怎么用"


def test_should_send_intro_for_help_keywords():
    assert should_send_intro("怎么用") is True
    assert should_send_intro("MaxRead") is True
    assert should_send_intro("普通聊天") is False


def test_intro_message_says_feedback_is_direct_chat():
    msg = intro_message("https://example.feishu.cn/docx/feedback")
    assert "目前支持" in msg
    assert "arXiv" in msg
    assert "直接私聊我" in msg
    assert "添加飞书表情反应" in msg
    assert "[在做了]：正在下载" in msg
    assert "[精神补给]：正在读" in msg
    assert "[思考]：正在审阅" in msg
    assert "[敲键盘]：正在写飞书文档" in msg
    assert "StatusReading" not in msg
    assert "[阅读中]" not in msg
    assert "[思考中]" not in msg
    assert "话题里更新进度" not in msg
    assert "example.feishu" not in msg


def test_group_intro_message_explains_group_usage_and_boundaries():
    msg = group_intro_message()
    assert "在群里用法" in msg
    assert "话题" in msg
    assert "添加飞书表情反应" in msg
    assert "[了解]" in msg
    assert "[在做了]：正在下载" in msg
    assert "[精神补给]：正在读" in msg
    assert "[思考]：正在审阅" in msg
    assert "OnIt" not in msg
    assert "StatusReading" not in msg
    assert "群里不 @ 我不会处理" in msg
    assert "不回溯整个群聊" in msg
    assert "能力边界" in msg

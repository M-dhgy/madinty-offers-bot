import bot


def test_campaign_taxonomy_codes_have_central_arabic_labels():
    for code, label in bot.CATEGORY_LABELS_AR.items():
        text = bot._campaign_preview_text({
            "ad_text": "نص الإعلان",
            "ai_data": {"category_code": code, "city_code": "KHARTOUM", "discount": 10},
        })
        assert label in text
        assert code not in text
    for code, label in bot.CITY_LABELS_AR.items():
        text = bot._campaign_preview_text({
            "ad_text": "نص الإعلان",
            "ai_data": {"category_code": "FOOD", "city_code": code, "discount": 10},
        })
        assert label in text
        assert code not in text


def test_action_labels_are_consistent_and_old_text_commands_remain_accepted():
    root_labels = {
        button.text
        for row in bot._user_reply_keyboard(type("Context", (), {"user_data": {}})()).keyboard
        for button in row
    }
    assert "📊 إحصاءات الإعلانات" in root_labels
    assert "🔄 تجديد الإعلان" in root_labels

    actions = bot._listing_actions_markup({
        "id": 9, "status": "approved", "image_file_ids": ["photo"],
    })
    action_labels = {button.text for row in actions.inline_keyboard for button in row}
    assert "📊 الإحصاءات" in action_labels
    assert "🔄 تجديد الإعلان" in action_labels
    assert not any("#إحصاء#" in text or "#تجديد#" in text for text in action_labels)
    assert "#إحصاء#" in bot.ROOT_KEYBOARD_TEXTS and "#تجديد#" in bot.ROOT_KEYBOARD_TEXTS

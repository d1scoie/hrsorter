"""ონლაინ მკითხველის ნიშნები („[ხელნაწერი: …]“, „[ბეჭედი: …]“): რა ითვლება ხელით მიწერად და რა — ბეჭდის სიტყვად.
მონაცემები გამოგონილია."""
from __future__ import annotations

import pytest

from hrsorter import readermarks as rm

NAMES = ["ნინო", "ბერიძე"]
STAMP = '[ბეჭედი: შპს „ალფა 2009“ GEORGIA "ALFA 2009" LTD, 200000008]'


def test_signature_stamp_and_own_name_are_not_handwritten_notes():
    """ნამდვილი ხელშეკრულების გვერდი: ორი ხელმოწერა და ბეჭედი ყოველ გვერდზე, ბოლოზე — ხელმომწერის ხელით დაწერილი სახელი."""
    page = ("1.1. დამსაქმებელი იღებს სტაჟიორს.\n"
            f"დამსაქმებელი [ხელნაწერი: ხელმოწერა] {STAMP}\nსტაჟიორი [ხელნაწერი: ხელმოწერა]\n"
            "[ხელნაწერი: ხელმოწერა, ბეჭედი: შპს ალფა 2009, 200000008]\n[ხელნაწერი: ნინო ბერიძე] [ხელნაწერი: ნ. ბერიძე]\n"
            "[ხელნაწერი: ხელმოწერა დამსაქმებლის მხარეს] [ხელნაწერი: ხელმოწერა, ნაწილობრივ ამოუკითხავი]\n"
            '[ხელნაწერი: ხელმოწერა და ბეჭედი "შპს ალფა 2009" 200000008] [ხელნაწერი: ხელმოწერა / ნ.ბერიძ?]')
    assert rm.handwritten(page, NAMES) == []
    assert rm.handwritten(page) == ["ნინო ბერიძე", "ნ. ბერიძე"]        # სახელების გარეშე უცნობია, ვისი სახელია
    assert rm.handwritten("ნაბეჭდი ტექსტი ნიშნების გარეშე") == [] and rm.handwritten(None) == []


@pytest.mark.parametrize("note", ["1500 ლარი", "12.05.2026", "ხელმოწერა, 1500 ლარი", "ხელმოწერა, არ ვეთანხმები",
                                  "გიორგი მაისურაძე", "?", ""])
def test_anything_else_written_by_hand_is_a_note(note):
    """თანხა, თარიღი, სხვისი სახელი, უარყოფა ან წაუკითხავი ხელნაწერი — ხელმოწერის აღწერაშიც ჩანს."""
    assert rm.handwritten(f"2.1. ხელფასი შეადგენს 1300 ლარს [ხელნაწერი: {note}] თვეში.", NAMES) == [note or "?"]


def test_reader_only_is_a_page_read_online_without_word_boxes():
    assert rm.reader_only({"text_source": "ocr:online:claude_code", "words": []})
    assert not rm.reader_only({"text_source": "ocr:online:claude_code", "words": [["სიტყვა", 0, 0, 1, 1]]})
    assert not rm.reader_only({"text_source": "ocr:online:claude_code:recheck", "words": []})    # ნიშნები Tesseract-ის ნიღბითაა
    assert not rm.reader_only({"text_source": "ocr:tesseract", "words": []})
    assert not rm.reader_only({"text_source": "text_layer"}) and not rm.reader_only({})


def test_stored_flag_is_ignored_only_on_reader_only_pages():
    """ნამდვილ ბაზაში ონლაინ მკითხველის 686 გვერდიდან 657-ს `handwriting_suspected=True` ეწერა — ნიღბის გარეშე გამოთვლილი."""
    online = {"text_source": "ocr:online:claude_code", "handwriting_suspected": True, "text": "ტექსტი [ხელნაწერი: ხელმოწერა]"}
    assert rm.handwriting_suspected(online) is False
    assert rm.handwriting_suspected(dict(online, text="ტექსტი [ხელნაწერი: 1500 ლარი]")) is True
    assert rm.handwriting_suspected(dict(online, text="[ხელნაწერი: ნინო ბერიძე]"), NAMES) is False
    assert rm.handwriting_suspected({"text_source": "ocr:tesseract", "handwriting_suspected": True, "text": ""}) is True
    assert rm.handwriting_suspected({"text_source": "text_layer", "handwriting_suspected": False, "text": "[ხელნაწერი: 1500]"}) is False


def test_stamp_words_come_from_every_stamp_marker_of_the_scan():
    pages = [f"ტექსტი {STAMP}", "[შტამპი: GEORGIA ALFA 2009 LTD 200000009]", "[ხელნაწერი: ხელმოწერა, ბეჭედი: შპს ალფა 2009]",
             "2.1. ხელფასი [ხელნაწერი: 1500 ლარი]", None]
    words = rm.stamp_words(pages)
    assert {"შპს", "ალფა", "2009", "georgia", "alfa", "ltd", "200000008", "200000009"} <= set(words)
    assert "1500" not in words and "ლარი" not in words and len(words) == len(set(words))
    assert rm.stamp_words(["ტექსტი ნიშნების გარეშე", ""]) == []

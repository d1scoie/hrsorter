from hrsorter import textquality as tq

GOOD = """ბრძანება № 15/2024
12.03.2024
შპს „მაგალითი“
ნინო ბერიძის (პ/ნ 01001000001) სამსახურში მიღების შესახებ.
ვბრძანებ: 1. ნინო ბერიძე მიღებულ იქნეს მენეჯერის თანამდებობაზე 2024 წლის 12 მარტიდან.
დირექტორი გიორგი კაპანაძე ხელმოწერა"""
LATIN1_GARBAGE = "ÀÁÂ ÃÄÅ ÆÇÈ ÉÊË ÌÍÎ ÏÐÑ ÒÓÔ ÕÖ× ØÙÚ ÛÜÝ Þßà áâã äåæ çèé êëì íîï ðñò"
PUA_GARBAGE = "     "
REPLACEMENT = "��� �� ���� ��� ����"
TRANSLIT = "Sromis xelSekruleba damsaqmebeli Sps magaliTi dasaqmebuli nino beriZe piradi nomeri xelmowera"
ENGLISH = "This is a plain English letter about the quarterly results of the company and its employees."


def test_good_georgian_scores_high():
    assert tq.score(GOOD) >= 0.85


def test_garbage_scores_low():
    assert tq.score(LATIN1_GARBAGE) < 0.3
    assert tq.score(PUA_GARBAGE) < 0.1
    assert tq.score(REPLACEMENT) < 0.1


def test_empty_is_zero():
    assert tq.score("") == 0.0
    assert tq.score("   \n\t ") == 0.0
    assert tq.score("---- .... ****") == 0.0


def test_short_text_penalised():
    assert tq.score("ბრძანება") < 0.5
    assert tq.score("ბრძანება") > 0.0


def test_ordering_and_thresholds():
    good, eng, tr, garbage = tq.score(GOOD), tq.score(ENGLISH), tq.score(TRANSLIT), tq.score(LATIN1_GARBAGE)
    assert good > eng > garbage
    assert good > tr
    assert 0.0 <= garbage <= 1.0 and 0.0 <= good <= 1.0


def test_mixed_garbage_reduces_score():
    mixed = GOOD + "\n" + PUA_GARBAGE * 6
    assert tq.score(mixed) < tq.score(GOOD) - 0.2


def test_unknown_markers_reduce_score():
    """ონლაინ OCR-ის „[?]“ (ამოუცნობი) მარკერები: ცოტა — თითქმის არ ცვლის, ბევრი — დაბალი ხარისხი."""
    base = tq.score(GOOD)
    few = GOOD.replace("მენეჯერის", "[?]")
    assert base - 0.1 < tq.score(few) < base
    words = GOOD.split()
    mostly = " ".join(w if i % 3 == 0 else "[?]" for i, w in enumerate(words))   # ~2/3 ამოუცნობი
    assert tq.score(mostly) < 0.35 < base

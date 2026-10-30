"""მართვადი შეცდომები. retryable=True — ხელახლა ცდა აზრიანია (დაბლოკილი ფაილი, API, დისკი)."""
from __future__ import annotations


class HRSorterError(Exception):
    def __init__(self, code: str, detail: str = "", retryable: bool = False):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail
        self.retryable = retryable


class IngestError(HRSorterError):
    """ინბოქსიდან არქივში მიღებისას (სტაბილურობა, კოპირება, ვალიდაცია)."""


class JobError(HRSorterError):
    """სამუშაოს (job) დამუშავებისას."""

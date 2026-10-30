"""საიდუმლოებების (API გასაღებები, პაროლები) შენახვა Windows Credential Manager-ში keyring-ით.

config.json-ში ინახება მხოლოდ საიდუმლოების სახელი (მაგ. "hrsorter/employee_api"), არასდროს მნიშვნელობა.
მნიშვნელობები არ იწერება ჟურნალში და არ ჩანს შეცდომის ტექსტში.

შენიშვნა: ფაილების სახელებში პირადი ნომრები შეიძლება მოხვდეს, ამიტომ `paths.root`-ზე (Sorted/, Proposed/,
NeedsReview/, _Archive/, _State/) წვდომა უნდა შეიზღუდოს Windows-ის NTFS უფლებებით (მხოლოდ HR ჯგუფი და
სერვისის ანგარიში). ეს მოდული ამას არ ცვლის — ეს ადმინისტრატორის კონფიგურაციაა.
"""
from __future__ import annotations

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

SERVICE = "hrsorter"


def get_secret(name: str) -> str | None:
    """აბრუნებს საიდუმლოებას ან None-ს (არ არსებობს / საცავი მიუწვდომელია)."""
    if not name:
        return None
    try:
        return keyring.get_password(SERVICE, name)
    except KeyringError:
        return None


def set_secret(name: str, value: str) -> None:
    if not name:
        raise ValueError("secret name is required")
    keyring.set_password(SERVICE, name, value)


def delete_secret(name: str) -> None:
    """წაშლა; არარსებული საიდუმლოება შეცდომად არ ითვლება."""
    try:
        keyring.delete_password(SERVICE, name)
    except PasswordDeleteError:
        pass

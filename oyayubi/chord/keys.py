"""US ANSI virtual-key → NICOLA-A key id."""

from __future__ import annotations

# Win32 VK
VK_SPACE = 0x20
VK_BACK = 0x08
VK_RETURN = 0x0D
VK_ESCAPE = 0x1B
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12  # Alt
VK_LWIN = 0x5B
VK_RWIN = 0x5C
VK_CAPITAL = 0x14
VK_OEM_1 = 0xBA  # ;
VK_OEM_PLUS = 0xBB
VK_OEM_COMMA = 0xBC
VK_OEM_MINUS = 0xBD
VK_OEM_PERIOD = 0xBE
VK_OEM_2 = 0xBF  # /
VK_OEM_3 = 0xC0
VK_OEM_4 = 0xDB  # [
VK_OEM_5 = 0xDC  # \
VK_OEM_6 = 0xDD  # ]
VK_OEM_7 = 0xDE  # '

# letter key id as in data/nicola_a.json
VK_TO_M: dict[int, str] = {
    0x51: "q",
    0x57: "w",
    0x45: "e",
    0x52: "r",
    0x54: "t",
    0x59: "y",
    0x55: "u",
    0x49: "i",
    0x4F: "o",
    0x50: "p",
    0x41: "a",
    0x53: "s",
    0x44: "d",
    0x46: "f",
    0x47: "g",
    0x48: "h",
    0x4A: "j",
    0x4B: "k",
    0x4C: "l",
    VK_OEM_1: ";",
    0x5A: "z",
    0x58: "x",
    0x43: "c",
    0x56: "v",
    0x42: "b",
    0x4E: "n",
    0x4D: "m",
    VK_OEM_COMMA: ",",
    VK_OEM_PERIOD: ".",
    VK_OEM_2: "/",
}

MODIFIER_VKS = {
    VK_SHIFT,
    VK_CONTROL,
    VK_MENU,
    VK_LWIN,
    VK_RWIN,
    0xA0,
    0xA1,
    0xA2,
    0xA3,
    0xA4,
    0xA5,  # L/R shift/ctrl/alt
}


def classify(vk: int) -> tuple[str, str | None]:
    """Return (kind, key_id). kind is M, O, MOD, EDIT, OTHER."""
    if vk == VK_SPACE:
        return "O", "space"
    if vk in VK_TO_M:
        return "M", VK_TO_M[vk]
    if vk in MODIFIER_VKS:
        return "MOD", None
    if vk in (VK_BACK, VK_RETURN, VK_ESCAPE):
        return "EDIT", None
    return "OTHER", None

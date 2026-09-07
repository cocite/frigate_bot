"""
Pretty object labels for the caption: emoji (shared) + a name in the chosen language.
The language is chosen in config.py (PRETTY_LABELS); "*" stands for any label missing from the dict.
To add a language, add another dict to LABEL_NAMES; to add a label, add a line to LABEL_EMOJI
and one to every language.
"""

LABEL_EMOJI = {
    "*": "❓",
    "person": "👤", "cat": "🐈", "dog": "🐕", "bird": "🐦", "car": "🚗",
    "face": "🧔‍♀️", "fox": "🦊", "bicycle": "🚲", "motorcycle": "🏍️", "bus": "🚌", "truck": "🚚",
}

LABEL_NAMES = {
    "ru": {"*": "НЕЧТО", "person": "ЧЕЛОВЕЧЕ", "cat": "КОШЕН", "dog": "СОБАКЕН", "bird": "ПТИЦА",
           "car": "МАШИНА", "face": "ФЭЙС", "fox": "ЛИС", "bicycle": "велосипед",
           "motorcycle": "мотоцикл", "bus": "автобус", "truck": "грузовик"},
    "en": {"*": "WTF", "person": "FELLA", "cat": "KITTY", "dog": "DOGGO", "bird": "BIRDIE",
           "car": "RIDE", "face": "MUG", "fox": "FOXY", "bicycle": "bike",
           "motorcycle": "motorbike", "bus": "bus", "truck": "truck"},
}

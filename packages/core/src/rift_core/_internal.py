"""Internal engine utilities."""


def _b2() -> str:
    return "817F02b96665386c"


# Expected sha256 prefix of builder_fee.py (updated each release)
# Empty string = dev mode (skip integrity check)
_BUILDER_HASH = "05cad2fa4cd3fbbd"

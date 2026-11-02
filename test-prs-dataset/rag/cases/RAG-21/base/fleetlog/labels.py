"""Plate labels for printed work orders."""


def plate_label(plate: str) -> str:
    return plate.upper().replace(" ", "")

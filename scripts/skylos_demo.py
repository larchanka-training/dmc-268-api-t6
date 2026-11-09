"""Throwaway demo for #263 (never merged): a new finding for the Skylos quality gate."""


def combine(a: int, b: int, c: int, d: int, e: int, f: int, g: int) -> int:
    return a + b + c + d + e + f + g


def unused_helper(value: int) -> int:
    return value - 1


if __name__ == "__main__":
    print(combine(1, 2, 3, 4, 5, 6, 7))

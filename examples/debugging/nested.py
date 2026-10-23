"""Deliberately small, public-safe Python debugger acceptance target."""

import sys


def leaf(value):
    result = value + 1  # LEAF_VALUE
    return result  # LEAF_RETURN


def outer(seed):
    adjusted = seed * 2  # OUTER_START
    result = leaf(adjusted)  # OUTER_CALL
    return result * 3  # OUTER_RETURN


def main():
    answer = outer(5)
    assert answer == 33
    if "--fail" in sys.argv:
        raise RuntimeError("deliberate debugger fixture exception")  # EXCEPTION
    print(f"fixture result: {answer}")


if __name__ == "__main__":
    main()

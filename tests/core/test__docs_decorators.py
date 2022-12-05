from great_expectations.core._docs_decorators import (
    deprecated,
    public_api,
)


@public_api
def _func_full_docstring_public_api(some_arg, other_arg):
    """My docstring.

    Longer description.

    Args:
        some_arg: describe some_arg
        other_arg: describe other_arg
    """
    pass

def _func_only_summary_public_api():
    """My docstring."""
    pass


def _func_no_docstring_public_api():
    pass



@deprecated(version="1.2.3", message="This is deprecated!!")
def _func_full_docstring_deprecated(some_arg, other_arg):
    """My docstring.

    Longer description.

    Args:
        some_arg: describe some_arg
        other_arg: describe other_arg
    """
    pass


@deprecated(version="1.2.3", message="This is deprecated!!")
def _func_only_summary_deprecated(some_arg, other_arg):
    """My docstring."""
    pass


@deprecated(version="1.2.3", message="This is deprecated!!")
def _func_no_docstring_deprecated(some_arg, other_arg):
    pass


def test_public_api_decorator():
    assert _func_full_docstring_public_api.__doc__ == (
        "--Public API--My docstring.\n"
        "\n"
        "    Longer description.\n"
        "\n"
        "    Args:\n"
        "        some_arg: describe some_arg\n"
        "        other_arg: describe other_arg\n"
        "    "
    )
    assert _func_full_docstring_public_api.__name__ == "_func_full_docstring_public_api"


def test_deprecated_decorator_full_docstring():

    assert _func_full_docstring_deprecated.__doc__ == (
        "My docstring.\n"
        "\n"
        ".. deprecated:: 1.2.3\n"
        "    This is deprecated!!\n"
        "\n"
        "\n"
        "Longer description.\n"
        "\n"
        "Args:\n"
        "    some_arg: describe some_arg\n"
        "    other_arg: describe other_arg\n"
    )


def test_deprecated_decorator_only_summary():

    assert _func_only_summary_deprecated.__doc__ == (
        "My docstring.\n"
        "\n"
        ".. deprecated:: 1.2.3\n"
        "    This is deprecated!!\n"
    )

def test_deprecated_decorator_no_docstring():

    assert _func_no_docstring_deprecated.__doc__ == (
        "\n"
        "\n"
        ".. deprecated:: 1.2.3\n"
        "    This is deprecated!!\n"
    )
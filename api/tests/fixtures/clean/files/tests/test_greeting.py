import pytest

from app.greeting import greet


@pytest.mark.parametrize(
    "name",
    [
        "Ada",
        "Grace",
        "Linus",
    ],
)
def test_greet_includes_name(name):
    assert name in greet(name)

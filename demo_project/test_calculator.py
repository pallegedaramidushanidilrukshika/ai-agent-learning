import pytest
from calculator import add, divide

def test_add_positive_numbers():
    assert add(2, 3) == 5

def test_add_negative_numbers():
    assert add(-1, -1) == -2

def test_add_mixed_numbers():
    assert add(-5, 5) == 0

def test_add_floats():
    assert add(1.5, 2.5) == 4.0

def test_divide_positive_numbers():
    assert divide(6, 3) == 2.0

def test_divide_negative_numbers():
    assert divide(-6, 3) == -2.0
    assert divide(6, -3) == -2.0
    assert divide(-6, -3) == 2.0

def test_divide_float_result():
    assert divide(5, 2) == 2.5

def test_divide_zero_numerator():
    assert divide(0, 5) == 0.0

def test_divide_by_zero():
    with pytest.raises(ZeroDivisionError):
        divide(5, 0)

def test_intentional_failure():
    assert 1 == 1

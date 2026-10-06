"""Demo: python -m siql.demo"""
from .models import Table

if __name__ == "__main__":
    t = Table()
    t.addCols(name=str, address=str, phone=str)
    t.add(name="John Doe", address="123 Place St.", phone="(592) 010-2345")
    t.add(name="Jimmy Doe", address="123 Place St. (Basement)")
    t.addCols(email=str)
    t.editByName("email", None, "email@placeholder.org")

    for i in t.select("John Doe"):
        print(i)

    for i in t.select("Jimmy Doe", "name"):
        print(i)

    print(t)

from .table import Table

class Command():
    SELECT = 0
    WHERE = 1
    FROM = 2
    AND = 3
    OR = 4
    ORDER = 5
    BY = 6

    def strToOp(s:str):
        match s:
            case "<":
                def x(l, r):
                    return l < r
            case ">":
                def x(l, r):
                    return l > r
            case ">=":
                def x(l, r):
                    return l >= r
            case "<=":
                def x(l, r):
                    return l <= r
            case "==":
                def x(l, r):
                    return l == r
            case "!=":
                def x(l, r):
                    return l != r

        return x

    def fromStr(s:str) -> int:
        match s.lower().strip():
            case "select":
                return 0
            case "where":
                return 1
            case "equals":
                return 2
            case "from":
                return 3
            case "and":
                return 4
            case "or":
                return 5
            case "if":
                return 6

class Interpreter():
    def Parse(query:str):
        for i in query.split(' '):
            c = Command.strToOp(i)


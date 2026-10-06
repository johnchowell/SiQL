def box(head: list[str], body: list[list[str]]) -> str:
    """Draw a text table with a header row and a rule between rows."""
    width = [max(len(head[j]), *(len(r[j]) for r in body), 0) for j in range(len(head))]

    def rule(left, mid, right):
        return left + mid.join("─" * (w + 2) for w in width) + right

    def line(cells):
        return "│" + "│".join(f" {v:<{w}} " for v, w in zip(cells, width)) + "│"

    sep = rule("├", "┼", "┤")
    out = [rule("┌", "┬", "┐"), line(head), sep]
    for n, r in enumerate(body):
        if n:
            out.append(sep)
        out.append(line(r))
    out.append(rule("└", "┴", "┘"))
    return "\n".join(out)

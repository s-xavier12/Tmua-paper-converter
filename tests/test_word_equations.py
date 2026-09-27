"""Patterns from Microsoft Word's equation editor (Cambria Math), rebuilt glyph by glyph:
fractions side by side, integral limits set to the right, maths-italic variables, prose in equations."""

from tmua_converter import mathlayout as ml

S = 11.0


def g(ch, x0, x1, base, size=S, raw=None, font="CambriaMath", space=False):
    h = 0.7 * size
    return ml.Glyph(x0, base - h, x1, base + 0.2 * size, base, size, ch, raw or ch, font, False, False,
                    True, 1, space_before=space)


def tex(glyphs, rules=()):
    problems = []
    items = ml.Builder(list(glyphs), list(rules), problems).build()
    lines = ml.build_lines(items, {}, S)
    return ml.lines_to_text(lines, problems)


def test_fractions_side_by_side():
    # ( 2/5 , 19/5 ) with small digits
    gl = [g("(", 101, 106.5, 153), g("2", 106.5, 111, 147, 8), g("5", 106.5, 111, 159, 8), g(",", 113, 115, 153.5),
          g("1", 117, 121.7, 147, 8), g("9", 121.7, 126, 147, 8), g("5", 119, 124, 159, 8), g(")", 126, 131.5, 153)]
    gl[0].y0, gl[0].y1 = 141, 159
    gl[-1].y0, gl[-1].y1 = 141, 159
    rules = [ml.Rule(106.5, 111.1, 150.4), ml.Rule(117, 126, 150.4)]
    assert tex(gl, rules) == "$\\left(\\frac{2}{5}, \\frac{19}{5}\\right)$"


def test_sin_next_to_maths_italic_variable():
    x = "\U0001d465"  # maths italic x
    gl = [g("s", 10, 14, 50), g("i", 14, 17, 50), g("n", 17, 22, 50), g("x", 22, 27, 50, raw=x),
          g("+", 30, 36, 50), g("c", 39, 43, 50), g("o", 43, 48, 50), g("s", 48, 52, 50), g("x", 52, 57, 50, raw=x)]
    assert tex(gl) == "$\\sin x + \\cos x$"


def test_sentence_typed_in_an_equation():
    words = "You may use the fact"
    gl, x = [], 10.0
    for k, w in enumerate(words.split()):
        for j, c in enumerate(w):
            gl.append(g(c, x, x + 6.5, 50, space=(j == 0 and k > 0)))  # italic boxes overlap: spacing from the PDF
            x += 5.5
        x += 1.0
    assert tex(gl) == words

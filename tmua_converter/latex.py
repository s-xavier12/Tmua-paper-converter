"""Lightweight LaTeX-in-text analysis used by the validator.

Strings follow the simulator convention: prose with inline maths in ``$...$``
and display maths in ``$$...$$``.  These helpers split a string into text and
maths segments and tokenise backslash commands, without needing a TeX engine.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Commands KaTeX understands that are common in exam papers.  Anything else is
# reported as "unrecognised" (a warning) so typos such as \frca surface.
KNOWN_COMMANDS = set("""
alpha beta gamma delta epsilon varepsilon zeta eta theta vartheta iota kappa lambda mu nu xi omicron pi varpi
rho varrho sigma varsigma tau upsilon phi varphi chi psi omega Gamma Delta Theta Lambda Xi Pi Sigma Upsilon Phi
Psi Omega digamma
frac dfrac tfrac cfrac binom dbinom tbinom sqrt root of over choose
sum prod coprod int iint iiint oint lim limsup liminf sup inf max min arg det gcd lcm deg dim ker hom Pr exp
log ln lg sin cos tan sec csc cot cosec arcsin arccos arctan sinh cosh tanh coth sech csch arcsec arccsc arccot
operatorname mod bmod pmod pod
le leq ge geq ne neq lt gt approx approxeq equiv sim simeq cong propto leqslant geqslant nleq ngeq nless ngtr
ll gg prec succ preceq succeq subset supset subseteq supseteq subsetneq supsetneq nsubseteq in notin ni owns
mid nmid parallel nparallel perp vdash models doteq
pm mp times div cdot ast star circ bullet oplus ominus otimes oslash odot cap cup setminus wedge vee land lor
lnot neg uplus sqcup sqcap amalg dagger ddagger
to gets rightarrow leftarrow Rightarrow Leftarrow leftrightarrow Leftrightarrow iff implies impliedby mapsto
longrightarrow longleftarrow Longrightarrow Longleftarrow longleftrightarrow Longleftrightarrow uparrow
downarrow Uparrow Downarrow updownarrow nearrow searrow swarrow nwarrow rightleftharpoons hookrightarrow
infty partial nabla forall exists nexists emptyset varnothing aleph hbar ell Re Im wp angle measuredangle
triangle square blacksquare Box diamond lozenge top bot surd prime backprime degree checkmark
therefore because cdots ldots dots vdots ddots dotsc dotsb dotsm dotsi
left right big Big bigg Bigg bigl bigr Bigl Bigr biggl biggr Biggl Biggr middle
langle rangle lfloor rfloor lceil rceil lvert rvert lVert rVert vert Vert lbrace rbrace lbrack rbrack
backslash
hat widehat bar overline underline vec overrightarrow overleftarrow tilde widetilde dot ddot acute grave
breve check mathring overbrace underbrace overset underset stackrel xrightarrow xleftarrow
text textrm textit textbf textsf texttt textnormal mbox hbox mathrm mathit mathbf mathsf mathtt mathcal
mathbb mathfrak mathscr boldsymbol bm pmb rm it bf sf tt cal displaystyle textstyle scriptstyle
scriptscriptstyle
quad qquad enspace thinspace medspace thickspace negthinspace negmedspace negthickspace hspace vspace
kern mkern mskip hskip phantom hphantom vphantom smash space nobreakspace
begin end hline cline substack atop brace brack
color textcolor colorbox boxed cancel bcancel xcancel sout not
underbar ulcorner urcorner llcorner lrcorner S P dag ddag copyright pounds yen euro textdegree
circledS diagup diagdown varpropto nleqslant ngeqslant lneq gneq lneqq gneqq ncong nsim nmid
""".split())

# Commands starting with "n": a "\n" that is not one of these is almost
# certainly a literal backslash-n that should have been a newline.
N_COMMANDS = {c for c in KNOWN_COMMANDS if c.startswith("n")}

# Commands whose (first) argument is prose, not maths.
TEXT_ARG_COMMANDS = {"text", "textrm", "textit", "textbf", "textsf", "texttt", "textnormal", "mbox", "hbox",
                     "operatorname", "mathrm", "mathit", "mathbf", "mathsf", "mathtt", "begin", "end",
                     "color", "textcolor"}


@dataclass(frozen=True)
class Segment:
    kind: str  # "text" | "inline" | "display"
    content: str
    start: int  # offset of content in the original string


@dataclass(frozen=True)
class DelimiterProblem:
    message: str
    offset: int


def _find_end_of_math(text: str, delim: str, start: int) -> int:
    """Port of KaTeX auto-render's findEndOfMath (brace-aware, skips escapes)."""
    index, brace = start, 0
    while index < len(text):
        ch = text[index]
        if brace <= 0 and text.startswith(delim, index):
            return index
        if ch == "\\":
            index += 1
        elif ch == "{":
            brace += 1
        elif ch == "}":
            brace -= 1
        index += 1
    return -1


def split_math(s: str) -> tuple[list[Segment], list[DelimiterProblem]]:
    """Split into text / inline-math / display-math segments.

    Mirrors KaTeX auto-render with the ``$$`` and ``$`` delimiters exactly:
    the next ``$`` in prose always opens maths (a preceding backslash does
    *not* escape it), ``$$`` opens display maths, and the closing delimiter is
    only recognised outside braces - so an unclosed ``{`` swallows the closing
    ``$`` and the rest of the string stays raw text.
    """
    segs: list[Segment] = []
    problems: list[DelimiterProblem] = []
    pos = 0
    while True:
        idx = s.find("$", pos)
        if idx == -1:
            break
        display = s.startswith("$$", idx)
        left = 2 if display else 1
        right = "$$" if display else "$"
        end = _find_end_of_math(s, right, idx + left)
        if end == -1:
            problems.append(DelimiterProblem(
                f"unclosed {right} delimiter (or an unbalanced brace hides the closing {right}) - the rest of the "
                "string would show as raw text", idx))
            break
        if idx > pos:
            segs.append(Segment("text", s[pos:idx], pos))
        if idx > 0 and s[idx - 1] == "\\":
            problems.append(DelimiterProblem("'\\$' does not escape a dollar sign for KaTeX auto-render - it opens "
                                             "maths", idx))
        content = s[idx + left:end]
        if display and re.search(r"(?<!\\)\$", content):
            problems.append(DelimiterProblem("single $ inside $$...$$ display maths", idx))
        if not content.strip():
            problems.append(DelimiterProblem("empty maths delimiters", idx))
        segs.append(Segment("display" if display else "inline", content, idx + left))
        pos = end + len(right)
    if pos < len(s):
        segs.append(Segment("text", s[pos:], pos))
    return segs, problems


@dataclass(frozen=True)
class Command:
    name: str  # letters, or a single symbol for \{ \, etc.
    offset: int
    backslashes: int  # length of the run of backslashes before the name


_CMD_RE = re.compile(r"(\\+)([A-Za-z]+|[^A-Za-z]?)")


def commands(s: str) -> list[Command]:
    return [Command(m.group(2), m.start(), len(m.group(1))) for m in _CMD_RE.finditer(s)]


def brace_balance(s: str) -> tuple[bool, str]:
    """Check {} nesting, ignoring escaped braces \\{ and \\}."""
    depth = 0
    i = 0
    while i < len(s):
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth < 0:
                return False, f"unexpected '}}' at offset {i}"
        i += 1
    if depth:
        return False, f"{depth} unclosed '{{'"
    return True, ""


def strip_text_arguments(math: str) -> str:
    """Remove \\text{...}-style arguments (prose) and all \\commands from maths."""
    out = []
    i = 0
    n = len(math)
    while i < n:
        if math[i] == "\\":
            m = re.match(r"\\([A-Za-z]+|.)", math[i:])
            name = m.group(1) if m else ""
            i += len(m.group(0)) if m else 1
            if name in TEXT_ARG_COMMANDS:
                j = i
                while j < n and math[j] == " ":
                    j += 1
                if j < n and math[j] == "{":
                    depth = 0
                    while j < n:
                        if math[j] == "\\":
                            j += 2
                            continue
                        if math[j] == "{":
                            depth += 1
                        elif math[j] == "}":
                            depth -= 1
                            if depth == 0:
                                j += 1
                                break
                        j += 1
                    i = j
            out.append(" ")
            continue
        out.append(math[i])
        i += 1
    return "".join(out)


def left_right_balance(math: str) -> bool:
    names = [c.name for c in commands(math) if c.backslashes == 1]
    return names.count("left") == names.count("right")


def env_balance(math: str) -> list[str]:
    problems = []
    stack: list[str] = []
    for m in re.finditer(r"\\(begin|end)\s*\{([^}]*)\}", math):
        kind, env = m.groups()
        if kind == "begin":
            stack.append(env)
        elif not stack or stack.pop() != env:
            problems.append(f"\\end{{{env}}} without matching \\begin")
    problems.extend(f"\\begin{{{env}}} without \\end" for env in stack)
    return problems

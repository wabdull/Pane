"""Tier 3 eval — 200-turn long session with real LLM speaker.

Runs a scripted math learning journey through multiple domains with
switches and returns. Measures context size, notional replay cost,
and savings at every turn. Proves the full system works end-to-end
with a real speaker over a long session.

Outputs a CSV with per-turn metrics and a final summary.

Run:
    python evals/tier3_longsession.py
    python evals/tier3_longsession.py --model claude-sonnet-4-6
    python evals/tier3_longsession.py --save evals/results/tier3
"""

import argparse
import csv
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

try:
    from anthropic import Anthropic
except ImportError:
    print("ERROR: pip install anthropic")
    sys.exit(1)

from pane.schema import (
    DEFAULT_TTL,
    USER_ENTITY,
    create_db,
    create_window,
    get_all_topics,
    get_entities_from_loaded_topics,
    get_facts_for_entities,
    get_loaded_topic_ids,
    get_loaded_topics_with_ttl,
    save_entity_fact,
    soft_load_recalled,
    tick_ttl,
)
from pane.recall import recall
from pane.chat import (
    PANE_SYSTEM_SUFFIX,
    build_context,
    extract_turn_json,
    notional_tokens,
    process_metadata,
)

SYSTEM_BASE = (
    "You are a math tutor helping a student relearn mathematics from "
    "foundations. Be concise but thorough. Use concrete examples. "
    "Track what the student knows and struggles with."
)

# 200-turn scripted conversation across math domains.
# Each entry: (domain_label, user_message)
# Domains switch naturally, with returns to prior topics.
CONVERSATION = []

def build_conversation():
    """Build the 200-turn scripted conversation."""
    turns = []

    # --- Algebra (1-20) ---
    algebra = [
        "alright so I need to relearn math from the ground up. I took calc in college but that was years ago and I forgot basically everything. lets start with algebra - the quadratic formula",
        "ok so its x = (-b +/- sqrt(b^2 - 4ac)) / 2a right? can you walk me through an example, maybe x^2 - 5x + 6 = 0",
        "wait so how do I know when to use the formula vs just factoring? like that one I could've just factored into (x-2)(x-3) right",
        "whats the discriminant part again? the b^2 - 4ac thing. what does that actually tell me",
        "oh interesting so if its negative theres no real solution? can you show me one like that",
        "ok switching gears, I remember factoring being important. teach me factoring polynomials from scratch",
        "let me try one: x^2 + 7x + 12. I need two numbers that multiply to 12 and add to 7... so 3 and 4? giving me (x+3)(x+4)",
        "what about x^2 - 9? theres no middle term",
        "oh right difference of squares! (x+3)(x-3). I actually remember that from school lol",
        "ok but what about something harder like x^3 - 8? I definitely don't remember how to factor cubics",
        "sum and difference of cubes... a^3 - b^3 = (a-b)(a^2+ab+b^2)? thats a lot to memorize",
        "someone mentioned the rational root theorem to me, what is that and when would I use it",
        "can you show me how to actually apply it? try 2x^3 - 3x^2 - 8x + 12",
        "ok once I find a root I need to do polynomial long division right? I vaguely remember that being tedious",
        "walk me through dividing x^3 - 2x^2 + x - 3 by x - 1 step by step, I want to make sure I understand the process",
        "is synthetic division the shortcut version? when can I use that instead",
        "hmm when would I use synthetic vs long division? does it matter",
        "let me try a harder one: factor 6x^2 - 7x - 20. this ones non-monic so its trickier right",
        "I got (2x - 5)(3x + 4)... let me check: 2x*3x = 6x^2, 2x*4 = 8x, -5*3x = -15x, -5*4 = -20. so 6x^2 + 8x - 15x - 20 = 6x^2 - 7x - 20. yes! nailed it",
        "ok I feel pretty solid on algebra basics. the factoring and quadratic formula are coming back to me. whats next",
    ]

    # --- Trigonometry (21-40) ---
    trig = [
        "alright lets do trig. I remember absolutely nothing about this except SOH CAH TOA and even that I'm fuzzy on",
        "ok so sin = opposite/hypotenuse, cos = adjacent/hypotenuse, tan = opposite/adjacent. can you show me on an actual triangle",
        "use a 3-4-5 right triangle so the numbers are clean",
        "now what's the unit circle? I remember my teacher drawing this big circle and putting all these fractions on it",
        "ugh theres so many values to remember. is there a trick or do I just have to memorize the whole thing",
        "oh wait the special triangles! 30-60-90 and 45-45-90. those generate all the common values right",
        "walk me through how to get sin(30) from the 30-60-90 triangle",
        "and sin(45) comes from the 45-45-90 one? so its 1/sqrt(2) which is sqrt(2)/2",
        "whats the relationship between sin and cos? I feel like theres something about them being shifts of each other",
        "teach me the pythagorean identity, I remember theres something with sin^2 + cos^2",
        "sin^2(x) + cos^2(x) = 1. ok that makes sense if you think about the unit circle right? x^2 + y^2 = 1 and cos is x, sin is y",
        "what other identities do I need to know? I remember there being a ton of them",
        "double angle formulas? like sin(2x) = 2sin(x)cos(x)? where does that come from",
        "ok what about law of sines. I remember using this in some class but I forget when",
        "when do I use law of sines vs law of cosines? I always got confused between them",
        "show me a law of cosines problem, something where I can't use law of sines",
        "also how do radians work? I know pi = 180 degrees but I never really understood WHY we use radians",
        "wait so thats literally just pi radians = 180 degrees? so 2pi is a full circle?",
        "why do mathematicians and physicists prefer radians over degrees? is it just convention or is there a real reason",
        "ok I think I have a decent foundation in trig now. some of the identities I'll need to practice more but the concepts make sense",
    ]

    # --- Calculus intro (41-60) ---
    calc1 = [
        "alright big moment... calculus. I took this in college and passed but I honestly don't think I ever truly understood it. lets start from scratch",
        "what even is a limit? like conceptually, not just the notation",
        "ok show me a simple one, like lim as x approaches 2 of x^2. I know the answer is 4 but walk me through the reasoning",
        "what about limits that dont exist? like lim of 1/x as x approaches 0",
        "now derivatives. I remember something about slopes and tangent lines but thats about it. explain it to me like I actually need to understand it this time",
        "the power rule was the main one right? bring down the exponent, subtract 1?",
        "let me try: derivative of x^3 is 3x^2. derivative of x^5 is 5x^4. am I doing this right",
        "ok harder one: derivative of 5x^4 - 3x^2 + 7. so 20x^3 - 6x + 0? the constant just disappears?",
        "now the chain rule is where I always got lost in college. whats the intuition behind it",
        "ok so for something like (2x+1)^3 I treat the outer function and inner function separately? derivative of outer times derivative of inner?",
        "what about product rule? f*g = f'g + fg' right?",
        "let me try: derivative of x^2 * sin(x). so 2x*sin(x) + x^2*cos(x)? that feels right",
        "and quotient rule is the annoying one... low d-high minus high d-low over low squared or something",
        "honestly when do I use each rule? like if I see a problem how do I know which rule to apply first",
        "whats the derivative of e^x? I remember it being special",
        "wait e^x is its own derivative?? thats wild. what about ln(x)",
        "what about trig derivatives? derivative of sin(x) is cos(x) right? and derivative of cos(x) is -sin(x)?",
        "the negative sign on the cos derivative always tripped me up. is there a way to remember which ones are negative",
        "what do second derivatives tell you? something about concavity?",
        "ok I think derivatives are clicking way better than they did in college. I think the issue before was I was just memorizing rules without understanding them",
    ]

    # --- RETURN to algebra (61-70) ---
    algebra_return = [
        "hey wait actually I just realized theres something from algebra I never properly learned - completing the square. we kind of skipped over it",
        "how does completing the square work? I know it has something to do with making a perfect square trinomial",
        "show me on x^2 + 6x + 5. walk through every step",
        "hmm so why would I ever complete the square when the quadratic formula exists? seems like extra work",
        "ohh for vertex form! so its useful for graphing parabolas and stuff, not just solving equations",
        "how does completing the square give me the vertex exactly? like algebraically whats the connection",
        "let me practice: complete the square for x^2 - 4x + 1. so... half of -4 is -2, square it to get 4. add and subtract 4: (x^2 - 4x + 4) + 1 - 4 = (x-2)^2 - 3",
        "so the vertex is (2, -3)? that was actually not that bad once you see the pattern",
        "could I have gotten the same answer with the quadratic formula? like -b/2a gives the x-coordinate of the vertex right",
        "yeah ok completing the square makes sense now. glad I came back to this. lets move on to something new",
    ]

    # --- Linear algebra (71-90) ---
    linalg = [
        "time for linear algebra. I never took this in college so this is all new to me. start with the basics - what even is a matrix",
        "ok so its just a grid of numbers in rows and columns. a 2x2 matrix has 2 rows and 2 columns. seems simple enough",
        "how do you add matrices? I'm guessing you just add the corresponding elements",
        "matrix multiplication is where it gets weird right? its not just element by element",
        "can you show me step by step: multiply [[1,2],[3,4]] by [[5,6],[7,8]]. I want to make sure I understand the row-by-column thing",
        "wait so AB is not the same as BA? matrix multiplication isnt commutative? thats so weird coming from regular algebra",
        "whats a determinant? I've heard the word but no idea what it actually means or why I'd care",
        "calculate the determinant of [[3,1],[2,4]]. so ad - bc = 3*4 - 1*2 = 10?",
        "what does it mean when the determinant is 0? my friend said something about the matrix being 'singular'",
        "ok now systems of linear equations. like 2x + y = 5 and x - y = 1. I can solve these by substitution but how do matrices help",
        "solve that system for me: 2x + y = 5, x - y = 1. both ways so I can see the comparison",
        "how do I use matrices specifically? like set up the augmented matrix and then what",
        "gaussian elimination... so I'm doing row operations to get it into a nice form?",
        "whats row echelon form? is that the staircase pattern thing",
        "what about inverse matrices? I've heard A^(-1) * A = I where I is the identity",
        "how do I actually find the inverse of a 2x2 matrix? is there a formula",
        "eigenvalues... I keep hearing this word in machine learning contexts. what are they actually",
        "why do eigenvalues matter? like whats the practical application",
        "ok and whats a vector space? this is where linear algebra starts getting really abstract right",
        "yeah this is getting pretty theoretical. I think I need to practice the computational stuff more before diving deeper into the theory",
    ]

    # --- RETURN to trig (91-100) ---
    trig_return = [
        "hey can we go back to trig real quick? theres some formulas I want to make sure I have down",
        "what was the double angle formula for sin again? I know we covered it but I can't remember",
        "right sin(2x) = 2sin(x)cos(x). let me write that down. and cos(2x) has multiple forms right?",
        "cos(2x) = cos^2(x) - sin^2(x) = 2cos^2(x) - 1 = 1 - 2sin^2(x). why are there three forms? when would I use each one",
        "wait theres THREE forms of the same identity?? thats confusing. how do I know which one to use in a problem",
        "show me an actual problem where I need the double angle formula so I can see it in context",
        "hmm can I find sin(60) using sin(2*30)? like sin(60) = 2*sin(30)*cos(30) = 2*(1/2)*(sqrt(3)/2) = sqrt(3)/2. nice that works!",
        "are there half angle formulas too? I vaguely remember those",
        "are the half angle formulas just derived from the double angle ones by solving backwards?",
        "ok yeah I feel better about trig identities now. the trick is really just knowing which form to use when. lets move on",
    ]

    # --- Calculus continued - integrals (101-120) ---
    calc2 = [
        "ok time for the other half of calculus - integrals. derivatives were about rates of change, integrals are about... area? accumulation?",
        "how are derivatives and integrals related exactly? I remember something about them being 'inverse operations'",
        "the fundamental theorem of calculus! I remember my professor making a big deal about this. explain it to me properly this time",
        "whats an antiderivative? is that literally just undoing a derivative",
        "so the integral of x^2 is x^3/3 + C? just reverse the power rule... add 1 to the exponent and divide by the new exponent",
        "integral of x^n in general is x^(n+1)/(n+1) + C. but wait what happens when n = -1? you'd get division by zero",
        "oh right thats where ln|x| comes from! integral of 1/x = ln|x| + C. I always thought that was random but now it makes sense",
        "whats the difference between definite and indefinite integrals? is it just whether you have bounds or not",
        "compute the integral from 0 to 2 of x^2 dx. so [x^3/3] from 0 to 2 = 8/3 - 0 = 8/3?",
        "u-substitution is like the chain rule in reverse right? how do I know when to use it",
        "let me try: integrate 2x * (x^2 + 1)^3 dx. if u = x^2 + 1 then du = 2x dx... so its integral of u^3 du = u^4/4 + C = (x^2+1)^4/4 + C",
        "integration by parts... uv - integral of v du. I remember the LIATE rule for choosing u. is that still the way to go",
        "when do I use parts vs substitution? is there a rule of thumb",
        "integrate x*e^x dx. let u = x, dv = e^x dx. then du = dx, v = e^x. so x*e^x - integral of e^x dx = x*e^x - e^x + C = e^x(x-1) + C",
        "trig substitution... this was the one that killed me in college. when do I actually need this",
        "honestly that seems really complicated. can you give me a simple case where I'd need it so its not so abstract",
        "integral of sin^2(x)... I need to use the identity sin^2(x) = (1 - cos(2x))/2 right? thats the half angle thing from trig coming back",
        "partial fractions! thats where you break apart a fraction into simpler ones right? like 1/(x^2-1) becomes something/(x-1) + something/(x+1)",
        "so integral of 1/(x^2-1) = (1/2)ln|x-1| - (1/2)ln|x+1| + C? using partial fractions and then integrating each piece",
        "man integrals are definitely harder than derivatives. with derivatives theres basically just a few rules. with integrals you have to be creative",
    ]

    # --- Probability (121-140) ---
    prob = [
        "lets switch to something different - probability. I need this for machine learning stuff and I never properly learned it",
        "start from the very beginning, whats a sample space",
        "coin flip probability is 1/2 for heads 1/2 for tails. simple enough. what about something slightly more complex",
        "rolling two dice - how many total outcomes are there? 6*6 = 36 right",
        "whats the probability of rolling a 7 with two dice? I need to count the favorable outcomes... 1+6, 2+5, 3+4, 4+3, 5+2, 6+1 = 6 ways. so 6/36 = 1/6",
        "conditional probability always confused me. P(A|B) means probability of A given B has happened? how is that different from regular probability",
        "whats the formula? P(A|B) = P(A and B) / P(B)? can you show me why that makes sense",
        "now Bayes theorem... this one comes up all the time in ML. P(A|B) = P(B|A)*P(A)/P(B). I can never remember it or understand why it works",
        "can you show me a concrete example? like a medical test scenario or something",
        "ok permutations vs combinations. permutations = order matters, combinations = order doesnt matter. but I always mix up the formulas",
        "how many ways to choose 3 people from 10 for a committee? order doesnt matter so thats C(10,3) = 10!/(3!*7!) = 120",
        "whats the binomial distribution? I know its related to coin flips somehow",
        "expected value is like the average outcome right? but its theoretical, not based on actual data",
        "variance and standard deviation... variance is E[(X-mu)^2] and standard deviation is just the square root of that?",
        "I always get confused about when to use population vs sample standard deviation. like n vs n-1 in the denominator",
        "the normal distribution is the bell curve. I know its characterized by mean and standard deviation but I never understood the actual equation",
        "what's the 68-95-99.7 rule? within 1 std dev is 68%, within 2 is 95%, within 3 is 99.7% of the data?",
        "how does that connect to z-scores? z = (x - mu) / sigma converts any normal distribution to standard normal right",
        "and p-values in hypothesis testing... thats the probability of getting a result as extreme as what you observed, assuming the null hypothesis is true?",
        "ok probability basics covered. this was actually really helpful for my ML understanding too",
    ]

    # --- RETURN to calculus (141-160) ---
    calc_return = [
        "I want to go back to calculus and push further. we covered derivatives and basic integrals but theres more right",
        "quick review first - power rule was d/dx[x^n] = nx^(n-1). chain rule was d/dx[f(g(x))] = f'(g(x))*g'(x). right?",
        "and we did product rule and quotient rule too. I think I have those down",
        "let me try something hard: whats the derivative of sin(x^2 * e^x). this looks like it needs chain rule AND product rule",
        "ok so the outer function is sin(...) and the inner is x^2 * e^x. derivative of sin is cos, then multiply by derivative of the inside which needs product rule...",
        "walk me through it step by step because I want to make sure I'm not making any mistakes",
        "now an integral challenge: integrate x^2 * cos(x) dx. this needs integration by parts right? and probably more than once",
        "ugh yeah integration by parts twice. thats tedious but I see how it works. each round reduces the power of x by 1",
        "what are improper integrals? integrals where one of the bounds is infinity?",
        "integrate 1/x^2 from 1 to infinity. so lim as b->inf of [-1/x] from 1 to b = lim (-1/b + 1) = 1. so it converges to 1!",
        "but integral of 1/x from 1 to infinity diverges right? even though 1/x goes to 0. thats counterintuitive",
        "whats the comparison test for determining if an integral converges or diverges",
        "now series and sequences. this was the end of calc 2 for me and where I really lost the thread. infinite sums blew my mind",
        "Taylor series - expanding a function as an infinite polynomial. e^x = 1 + x + x^2/2! + x^3/3! + ...",
        "derive the Taylor series for e^x from scratch. I want to see where those factorials come from",
        "and Taylor series for sin(x)? thats the one with alternating signs right",
        "why are Taylor series actually useful? like when would an engineer or scientist use one",
        "whats the radius of convergence? some series only work for certain values of x?",
        "this connects back to limits right? the whole foundation of calculus is limits and here we are again with limits of partial sums",
        "calculus is honestly beautiful once you see how everything connects. I wish I had appreciated it more in college",
    ]

    # --- Geometry (161-180) ---
    geom = [
        "I want to do some geometry. its been forever since I thought about shapes and proofs and stuff",
        "start with angles - acute is less than 90, right is 90, obtuse is between 90 and 180. what about reflex angles",
        "wait reflex angles are between 180 and 360? I dont think I ever learned about those",
        "properties of triangles - the angles sum to 180 degrees. I remember that. what else is fundamental",
        "how do I prove the angles of a triangle sum to 180? is there an easy proof",
        "congruent vs similar triangles. congruent = same shape and size, similar = same shape different size. right?",
        "what are the triangle congruence tests? SAS SSS ASA... are there others",
        "area of a triangle is (1/2) * base * height. but what if I dont know the height? thats where the trig formula comes in right, (1/2)ab*sin(C)",
        "oh wait the pythagorean theorem! a^2 + b^2 = c^2 for right triangles. this connects to the trig stuff we did earlier",
        "can you show me an elegant proof of the pythagorean theorem? I've heard there are hundreds of proofs",
        "area of a circle is pi*r^2 and circumference is 2*pi*r. but where does pi actually come from? like why is it that specific number",
        "huh so pi is just the ratio of circumference to diameter for ANY circle? and its irrational so it never repeats or terminates. thats wild",
        "volume of a sphere is (4/3)*pi*r^3. I always just memorized this but can you derive it using calculus? like integration",
        "oh thats cool, so volumes of revolution from calculus give you the sphere formula. everything connects",
        "surface area of a cylinder is 2*pi*r*h + 2*pi*r^2. the first part is the side (rectangle wrapped around) and the second is the two circles on top and bottom",
        "what about regular polygons? a regular n-gon has n equal sides and n equal angles. what are the interior angles",
        "interior angle = (n-2)*180/n. so for a hexagon its (6-2)*180/6 = 120 degrees. does that mean regular hexagons tessellate?",
        "coordinate geometry is where algebra meets geometry right? distance formula, midpoint formula, equations of lines and circles",
        "the distance formula is sqrt((x2-x1)^2 + (y2-y1)^2). but wait thats literally just the pythagorean theorem in coordinate form!",
        "I love how these different branches of math keep connecting to each other. pythagorean theorem shows up everywhere",
    ]

    # --- Mixed review / rapid switches (181-200) ---
    mixed = [
        "ok lets do a rapid fire review across everything we've covered. hit me with questions from different topics",
        "whats the quadratic formula again? x = (-b +/- sqrt(b^2-4ac)) / 2a. boom, still got it",
        "derivative of x^4 is 4x^3. easy",
        "sin(pi/6) = sin(30) = 1/2. from the 30-60-90 triangle",
        "determinant of [[1,2],[3,4]] = 1*4 - 2*3 = 4 - 6 = -2. negative determinant means the matrix reverses orientation right?",
        "integral of e^x dx = e^x + C. because e^x is its own derivative",
        "probability of drawing 2 aces from a deck... thats C(4,2)/C(52,2) = 6/1326 = 1/221",
        "area of a circle with radius 5 is pi*25 = 25pi. approximately 78.5",
        "factor x^2 - 16 = (x-4)(x+4). difference of squares again",
        "chain rule: d/dx[f(g(x))] = f'(g(x)) * g'(x). derivative of the outside times derivative of the inside",
        "eigenvalues of [[2,0],[0,3]] are just 2 and 3 right? because its diagonal. det(A - lambda*I) = 0 gives (2-lambda)(3-lambda) = 0",
        "law of cosines: c^2 = a^2 + b^2 - 2ab*cos(C). generalizes pythagorean theorem for non-right triangles",
        "Taylor series for cos(x) = 1 - x^2/2! + x^4/4! - x^6/6! + ... alternating signs on even powers",
        "gaussian elimination is row reduction to get a matrix into echelon form to solve a system of equations",
        "sin^2(x) + cos^2(x) = 1. the pythagorean identity. comes from x^2 + y^2 = 1 on the unit circle",
        "fundamental theorem of calculus: integral from a to b of f(x)dx = F(b) - F(a) where F is an antiderivative",
        "vertex form of a parabola is y = a(x-h)^2 + k where (h,k) is the vertex. get there by completing the square",
        "integrate sin(x)*cos(x) dx... hmm u = sin(x), du = cos(x)dx so integral of u du = u^2/2 = sin^2(x)/2 + C. or I could use the double angle identity, sin(2x)/2",
        "P(A or B) = P(A) + P(B) - P(A and B). subtract the intersection to avoid double counting",
        "man that was intense but actually really satisfying. I can tell how much I've improved since we started. everything connects - algebra feeds into trig feeds into calculus feeds into linear algebra. math is one big web",
    ]

    domains = [
        ("algebra", algebra),
        ("trig", trig),
        ("calculus-derivatives", calc1),
        ("algebra-return", algebra_return),
        ("linear-algebra", linalg),
        ("trig-return", trig_return),
        ("calculus-integrals", calc2),
        ("probability", prob),
        ("calculus-return", calc_return),
        ("geometry", geom),
        ("mixed-review", mixed),
    ]

    for domain_label, msgs in domains:
        for msg in msgs:
            turns.append((domain_label, msg))

    return turns


def main():
    parser = argparse.ArgumentParser(description="Tier 3: 200-turn long session eval")
    parser.add_argument("--model", default="claude-sonnet-4-6")
    parser.add_argument("--save", default=None, help="directory to save results")
    parser.add_argument("--turns", type=int, default=None,
                        help="limit number of turns (default: all 200)")
    args = parser.parse_args()

    if not os.getenv("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set")
        sys.exit(1)

    conversation = build_conversation()
    if args.turns:
        conversation = conversation[:args.turns]

    client = Anthropic()
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(db_fd)
    db = create_db(db_path)
    window_id = create_window(db)
    system_prompt = SYSTEM_BASE + PANE_SYSTEM_SUFFIX

    # Seed user facts
    save_entity_fact(db, USER_ENTITY, "name", "Waleed")
    save_entity_fact(db, USER_ENTITY, "goal", "relearn math for masters/PhD")
    save_entity_fact(db, USER_ENTITY, "background", "CS degree, rusty on math")

    messages = []  # API message history (recent window)
    recent_messages = []
    RAW_WINDOW = 10
    turn_data = []
    total_in = 0
    total_out = 0

    print(f"Tier 3 long-session eval | model: {args.model} | {len(conversation)} turns")
    print("=" * 72)

    start_time = time.time()

    for i, (domain, user_msg) in enumerate(conversation, 1):
        # 1. Tick TTL
        tick_ttl(db)

        # 2. Soft-load recalled topics
        result = recall(user_msg, db)
        matched = [t["id"] for t, _ in result.topics[:5]] if result.topics else []
        if matched:
            soft_load_recalled(db, matched)

        # 3. Build context
        memory_block = build_context(db)
        pane_tokens = len(memory_block) // 4
        for msg in recent_messages[-RAW_WINDOW:]:
            pane_tokens += len(msg.get("content", "")) // 4

        # 4. Assemble messages for API
        api_messages = []
        if memory_block:
            api_messages.append({
                "role": "user",
                "content": f"[CONTEXT — background memory]\n{memory_block}"
            })
            api_messages.append({
                "role": "assistant",
                "content": "Understood, I have the background context."
            })
        api_messages.extend(recent_messages[-RAW_WINDOW:])
        api_messages.append({"role": "user", "content": user_msg})

        # 5. API call
        try:
            response = client.messages.create(
                model=args.model,
                system=[{
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }],
                messages=api_messages,
                max_tokens=2000,
            )
        except Exception as e:
            print(f"\n[Turn {i}] API ERROR: {e}")
            break

        assistant_text = response.content[0].text
        in_tok = response.usage.input_tokens
        out_tok = response.usage.output_tokens
        cache_read = getattr(response.usage, "cache_read_input_tokens", 0) or 0
        total_in += in_tok
        total_out += out_tok

        # 6. Extract metadata and process
        metadata, clean_text = extract_turn_json(assistant_text)
        if metadata:
            action = process_metadata(db, window_id, user_msg,
                                      assistant_text, metadata)
        else:
            from pane.schema import save_messages
            save_messages(db, window_id, [
                {"role": "user", "content": user_msg},
                {"role": "assistant", "content": assistant_text},
            ])
            action = "no-metadata"

        # 7. Track recent messages
        recent_messages.append({"role": "user", "content": user_msg})
        recent_messages.append({"role": "assistant", "content": clean_text})

        # 8. Metrics
        nt = notional_tokens(db)
        loaded = get_loaded_topics_with_ttl(db)
        active = get_entities_from_loaded_topics(db)
        saving_pct = round((1 - pane_tokens / nt) * 100) if nt > 0 else 0

        turn_data.append({
            "turn": i,
            "domain": domain,
            "user_message": user_msg,
            "assistant_response": clean_text,
            "action": action,
            "in_tokens": in_tok,
            "out_tokens": out_tok,
            "cache_read": cache_read,
            "pane_context_tokens": pane_tokens,
            "notional_tokens": nt,
            "saving_pct": max(0, saving_pct),
            "loaded_topics": len(loaded),
            "active_entities": len(active),
            "total_topics_in_db": len(get_all_topics(db)),
        })

        # Progress output
        if i % 10 == 0 or i == 1:
            print(f"  Turn {i:3d} [{domain:22s}] {action:10s} | "
                  f"pane: {pane_tokens:5,} | notional: {nt:6,} | "
                  f"saving: {max(0,saving_pct):3d}% | "
                  f"loaded: {len(loaded)} | entities: {len(active)}")

    elapsed = time.time() - start_time

    # === Summary ===
    print("\n" + "=" * 72)
    print("SUMMARY")
    print("=" * 72)

    completed = len(turn_data)
    final = turn_data[-1] if turn_data else {}
    topics = get_all_topics(db)

    print(f"\n  Turns completed: {completed}")
    print(f"  Total tokens: {total_in:,} in / {total_out:,} out "
          f"({total_in + total_out:,} total)")
    print(f"  Time: {elapsed:.0f}s ({elapsed/completed:.1f}s/turn)")
    print(f"  Topics in DB: {len(topics)}")
    print(f"  Final context: {final.get('pane_context_tokens', 0):,} tokens")
    print(f"  Final notional: {final.get('notional_tokens', 0):,} tokens")
    print(f"  Final saving: {final.get('saving_pct', 0)}%")

    # Savings over time
    print(f"\n  Savings progression:")
    for td in turn_data:
        if td["turn"] in [1, 10, 20, 40, 60, 80, 100, 120, 140, 160, 180, 200]:
            print(f"    Turn {td['turn']:3d}: {td['saving_pct']:3d}% "
                  f"(pane: {td['pane_context_tokens']:,} vs "
                  f"notional: {td['notional_tokens']:,})")

    # Actions breakdown
    actions = {}
    for td in turn_data:
        actions[td["action"]] = actions.get(td["action"], 0) + 1
    print(f"\n  Actions: {actions}")

    # No-metadata rate
    no_meta = actions.get("no-metadata", 0)
    if no_meta:
        print(f"  WARNING: {no_meta} turns had no metadata "
              f"({no_meta/completed*100:.0f}%)")

    # Save results
    if args.save:
        save_dir = Path(args.save)
        save_dir.mkdir(parents=True, exist_ok=True)

        # CSV
        csv_path = save_dir / "tier3_metrics.csv"
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=turn_data[0].keys())
            writer.writeheader()
            writer.writerows(turn_data)
        print(f"\n  CSV saved to {csv_path}")

        # JSON summary
        summary = {
            "model": args.model,
            "turns": completed,
            "total_in": total_in,
            "total_out": total_out,
            "elapsed_seconds": elapsed,
            "final_pane_tokens": final.get("pane_context_tokens", 0),
            "final_notional_tokens": final.get("notional_tokens", 0),
            "final_saving_pct": final.get("saving_pct", 0),
            "topics_in_db": len(topics),
            "actions": actions,
            "turn_data": turn_data,
        }
        # Readable chat log
        log_path = save_dir / "tier3_chat.md"
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(f"# Tier 3 Chat Log — {completed} turns\n\n")
            f.write(f"Model: {args.model}\n\n---\n\n")
            for td in turn_data:
                f.write(f"### Turn {td['turn']} — {td['domain']}\n\n")
                f.write(f"**You:** {td['user_message']}\n\n")
                f.write(f"**Claude:** {td['assistant_response']}\n\n")
                f.write(f"*[{td['action']}] {td['in_tokens']:,} in / "
                        f"{td['out_tokens']:,} out | "
                        f"pane: {td['pane_context_tokens']:,} | "
                        f"notional: {td['notional_tokens']:,} | "
                        f"saving: {td['saving_pct']}%*\n\n---\n\n")
        print(f"  Chat log saved to {log_path}")

        json_path = save_dir / "tier3_summary.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)
        print(f"  JSON saved to {json_path}")

    db.close()
    os.remove(db_path)


if __name__ == "__main__":
    main()

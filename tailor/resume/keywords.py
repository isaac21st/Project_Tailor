"""The keywords an applicant tracking system scores a resume on, read from one posting, and a Jobscan-style match score.

This follows the person's tailoring guide (2026-10-06, "How to Tailor Your Resume with AI in 2026"):
  step 1  extract the posting's keywords in four kinds:
            hard      tools, methods and techniques ("SQL", "user research", "A/B testing")
            industry  the field's own terms ("healthcare", "life sciences", "fintech", "B2B")
            soft      ways of working ("cross-functional collaboration", "communication")
            verb      what the role does: the first word of each duty ("develop", "synthesize", "drive")
  step 3  score the resume against them (aim for 75%+) and list what is missing.

Every vocabulary term knows how postings say it (several wordings) and, for terms that may be ADDED to a resume, what
in a resume shows the person really has it (evidence). A term is only ever added where that evidence is there; a term
without evidence is only reported ("missing: add it only if it is true"). Tools the posting names that the vocabulary
does not know (CamelCase names, acronyms) are scored and reported, never added.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..jobs.text import CleanJob


@dataclass(frozen=True)
class Term:
    name: str  # as written on a resume ("Data visualization")
    kind: str  # hard | industry | soft
    posting: str  # regex: how postings (and resumes) say it
    evidence: str = ""  # regex over the base resume: what shows the person has it; "" = never added, only scored
    line: str = ""  # the skills line it joins when added ("Analytics"); "" = never added
    resume: str = ""  # regex: how a resume shows it, when looser than the posting's wording (soft skills)

    def rx(self) -> re.Pattern:
        return _compiled(self.posting)

    def on_resume(self) -> re.Pattern:
        return _compiled(self.resume or self.posting)

    def ev(self) -> re.Pattern | None:
        return _compiled(self.evidence) if self.evidence else None


_CACHE: dict[str, re.Pattern] = {}


def _compiled(p: str) -> re.Pattern:
    if p not in _CACHE:
        _CACHE[p] = re.compile(p, re.I)
    return _CACHE[p]


CS, AN, TECH, AI, UX = "Consulting & Strategy", "Analytics", "Technology", "AI Tools", "UX Research"

# Evidence patterns are generic statements of what in a resume shows the skill, so they work for any resume; they were
# checked against the person's resume on 2026-10-06 (expert interviews -> primary research, Matplotlib -> data
# visualization, 9 interactive views -> dashboards, HubSpot -> CRM ...).
VOCAB: list[Term] = [
    # ---- research and analysis methods
    Term("Data analysis", "hard", r"\bdata analy[sz]|\banaly[sz](?:e|ing|ed) (?:large )?(?:data|datasets)\b|analytical skills",
         r"analy[sz]\w* (?:data|\d[\d,]* \w+ records)|data analytics|\bdatasets?\b", AN),
    Term("Data visualization", "hard", r"data visuali[sz]ation|visuali[sz]e data|visuali[sz]ations?\b",
         r"visuali[sz]ation|matplotlib|tableau|power bi", AN),
    Term("Dashboards", "hard", r"\bdashboards?\b", r"\bdashboards?\b|interactive views", AN),
    Term("Quantitative analysis", "hard", r"\bquantitative\b", r"analy[sz]\w* \d[\d,]* |\d+(?:\.\d+)?% over|data analytics", AN),
    # interviews synthesized into findings; not a literature synthesis ("synthesizing evidence" in a manuscript, 2026-10-07)
    Term("Qualitative research", "hard", r"\bqualitative\b", r"interviews?\b.*synthesi|synthesi\w* findings", UX),
    Term("User research", "hard", r"\buser research|\bux research|\buser insights?\b|\bcustomer research\b|\bdesign research\b|talk(?:ing)? to (?:users|customers)",
         r"user interviews|usability testing|virtual interviews|ux research", UX),
    Term("Usability testing", "hard", r"usability (?:testing|studies|tests)|user testing", r"usability testing", UX),
    Term("User interviews", "hard", r"(?:user|customer|stakeholder|expert) interviews|conduct(?:ing)? interviews",
         r"interviews", UX),
    Term("Prototyping", "hard", r"\bprototyp", r"prototyp|wireframing", UX),
    Term("Wireframing", "hard", r"wirefram", r"wirefram", UX),
    Term("Personas", "hard", r"\bpersonas?\b", r"\bpersonas\b", UX),
    Term("Journey mapping", "hard", r"journey map"),
    Term("Information architecture", "hard", r"information architecture", r"information architecture", UX),
    Term("Survey design", "hard", r"\bsurveys?\b", r"\bsurveys?\b"),
    Term("Research synthesis", "hard", r"\bsynthesi[sz]", r"synthesi[sz]", UX),
    Term("Market research", "hard", r"market research|market analysis|market (?:landscape|assessment)s?",
         r"market research|national benchmarks|industry trend", CS),
    Term("Competitive analysis", "hard", r"competitive (?:analysis|landscape|intelligence)|competitor analysis",
         r"competitive analysis", CS),
    Term("Primary research", "hard", r"primary research", r"(?:expert|virtual|user) interviews", CS),
    Term("Secondary research", "hard", r"secondary research|desk research|literature (?:review|search)|medical literature",
         r"\d+ (?:research )?articles|research articles|literature", CS),
    Term("Benchmarking", "hard", r"\bbenchmark", r"\bbenchmarks?\b", CS),
    Term("Trend analysis", "hard", r"trend analysis|industry trends|market trends", r"trend analysis|industry trend", CS,
         resume=r"trend analysis|industry trend|market trend"),  # "industry trend and competitive analysis" says it
    Term("Market sizing", "hard", r"market sizing|\btam\b|total addressable"),
    Term("Due diligence", "hard", r"due diligence"),
    Term("Financial modeling", "hard", r"financial model|valuation|\bdcf\b"),
    Term("Statistical analysis", "hard", r"statistic(?:s|al)|regression|hypothesis test"),
    Term("A/B testing", "hard", r"a/b test|split test|experimentation|experiments?\b"),
    Term("Machine learning", "hard", r"machine learning|\bml\b"),
    # ---- strategy, product, operations
    Term("Product strategy", "hard", r"product strateg|product vision", r"product[- ]strategy|strategy and execution|brand strategy", CS),
    Term("Roadmapping", "hard", r"\broadmaps?\b"),
    Term("Product requirements", "hard", r"product requirements|\bprds?\b|requirements (?:gathering|definition|documents?)|"
         r"write specifications|specifications|\bspecs\b"),
    Term("User stories", "hard", r"user stor"),
    Term("Go-to-market strategy", "hard", r"go-to-market|\bgtm\b"),
    Term("Conversion optimization", "hard", r"conversion|funnel|growth (?:experiments|metrics)",
         r"conversion \d+%|lifting \w+ conversion", CS),
    Term("Onboarding design", "hard", r"\bonboarding\b", r"onboarding flow|simplified onboarding", UX),
    Term("Brand strategy", "hard", r"\bbrand(?:ing)? strateg|brand system|\bbranding\b", r"brand system|brand strategy", CS),
    Term("Customer segmentation", "hard", r"segmentation", r"segmentation", CS),
    Term("Customer insights", "hard", r"customer insights?|consumer insights?|customer needs|user needs|community needs",
         r"customer (?:behavior|needs)|user needs", CS),
    Term("Pricing strategy", "hard", r"pricing (?:strategy|strategies|analytics|optimi[sz]ation)|monetization"),
    Term("Operating-model design", "hard", r"operating[- ]models?|target operating", r"operating-model", CS),
    Term("Process improvement", "hard", r"process improvement|continuous improvement|process (?:optimi[sz]ation|redesign)|"
         r"operational (?:improvement|excellence|efficiency)|workflow (?:optimi[sz]ation|improvement)",
         r"operational bottlenecks|cross-functional improvements", CS),
    Term("Facility planning", "hard", r"facilit(?:y|ies) planning|space planning|capital planning|facility design",
         r"floor-by-floor layouts|planning framework for a [\d,–-]+-square-foot", CS),
    Term("Project management", "hard", r"project management|manage (?:multiple )?projects|workstreams?|project plans?|"
         r"keep projects moving|deadlines",
         r"led the [\w ]*workstream|strategy and execution end-to-end", CS),
    Term("Executive presentations", "hard", r"client presentations?|executive presentations?|present(?:ing)? (?:findings |results )?to "
         r"(?:clients|executives|senior|leadership)|presentations? to (?:clients|executives|senior)|prepare and deliver",
         r"presented [\w -]*recommendations to", CS),
    Term("Strategic recommendations", "hard", r"strategic recommendations|recommendations", r"recommendations?\b", CS),
    Term("Stakeholder management", "soft", r"stakeholder management|manag(?:e|ing) stakeholders",
         r"stakeholder|presented [\w -]*to [\w ]*leaders", CS),
    Term("Stakeholder communication", "soft", r"stakeholders?", r"stakeholder", CS),
    Term("Cross-functional collaboration", "soft", r"cross[- ]functional", r"cross-functional", CS),
    Term("Customer experience", "hard", r"customer experience|\bcx\b|member experience|patient experience|client experience",
         r"net promoter score|customer needs", CS),
    Term("Healthcare operations", "hard", r"health(?:care)? operations|care delivery|clinical operations|hospital operations|"
         r"provider operations", r"care center|operating-model and planning framework", CS),
    Term("Data cleaning", "hard", r"data clean|data cleans|clean(?:ing)? (?:the )?data|data quality|data preparation|data wrangling",
         r"data-cleanup|standardized dataset|validating every output", AN),
    Term("Commercial strategy", "hard", r"commercial (?:strategy|strategies|excellence|planning)|commerciali[sz]ation"),
    Term("Market access", "hard", r"market access|reimbursement|pricing and access|\bheor\b"),
    Term("Workshop facilitation", "hard", r"facilitat\w* (?:\w+ )?workshops?|workshop facilitation"),
    Term("Change management", "hard", r"change management|organi[sz]ational change"),
    Term("Hypothesis-driven problem solving", "hard", r"hypothesis[- ]driven|issue trees?|structured problem[- ]solving"),
    Term("KPIs", "hard", r"\bkpis?\b|key performance indicators|\bokrs?\b"),
    Term("Revenue growth", "hard", r"revenue (?:growth|generation)|grow revenue|drive revenue", r"\$[\d,.]+ (?:million )?in (?:annual )?revenue"),
    # ---- tools (scored; added only when the resume already names them, which means they are already there)
    Term("Excel", "hard", r"\bexcel\b"),
    Term("PowerPoint", "hard", r"powerpoint|\bslides\b|google slides|keynote"),
    Term("Microsoft Office", "hard", r"microsoft office|ms office|office suite|microsoft 365"),
    Term("SQL", "hard", r"\bsql\b"),
    Term("Python", "hard", r"\bpython\b"),
    Term("R", "hard", r"\bR\b(?: programming| studio|,)|\bRStudio\b"),
    Term("Tableau", "hard", r"\btableau\b"),
    Term("Power BI", "hard", r"power ?bi\b"),
    Term("Looker", "hard", r"\blooker\b"),
    Term("Figma", "hard", r"\bfigma\b"),
    Term("Jira", "hard", r"\bjira\b"),
    Term("Confluence", "hard", r"\bconfluence\b"),
    Term("Salesforce", "hard", r"\bsalesforce\b"),
    Term("CRM", "hard", r"\bcrm\b|customer relationship management", r"hubspot|gohighlevel|salesforce", TECH),
    Term("Google Analytics", "hard", r"google analytics"),
    Term("Amplitude", "hard", r"\bamplitude\b"),
    Term("Mixpanel", "hard", r"\bmixpanel\b"),
    Term("Qualtrics", "hard", r"\bqualtrics\b"),
    Term("HTML/CSS", "hard", r"\bhtml\b|\bcss\b"),
    Term("JavaScript", "hard", r"javascript"),
    Term("Agile", "hard", r"\bagile\b|\bscrum\b|sprints?\b"),
    # not a bare "AI": nearly every 2026 posting says it in passing ("our AI-first culture")
    Term("Generative AI", "hard", r"generative ai|\bgen ?ai\b|\bllms?\b|large language models?|\bai tools?\b|ai-powered|"
         r"\bartificial intelligence\b|\bai (?:products?|features?|models?|solutions?|platforms?|systems?|agents?|"
         r"applications?|workflows?|adoption|strategy|transformation)\b|\b(?:apply|use|using|leverage|build(?:ing)? with) ai\b",
         r"claude|gemini|chatgpt|ai-assisted|\bllm", AI, resume=r"generative ai|claude|gemini|chatgpt|ai-assisted|\bllm|\bai tools\b"),
    # ---- industries
    Term("Healthcare", "industry", r"health ?care|health systems?|hospitals?|providers?\b|payers?\b|clinical|clinicians?|patients?\b",
         r"health\b|care center|clinical", CS, resume=r"health\b|care center|clinical|patient"),
    Term("Life sciences", "industry", r"life sciences|pharma(?:ceutical)?s?\b|biotech|medical devices?|diagnostics"),
    Term("Fintech", "industry", r"fintech|financial services|payments?\b|banking", r"fintech|investor platform",
         resume=r"fintech|investor|\bipo\b"),
    Term("Consumer", "industry", r"\bconsumer\b|\bretail\b|\bcpg\b", r"customer|retail", resume=r"consumer|customer|retail"),
    Term("B2B", "industry", r"\bb2b\b|enterprise customers"),
    Term("SaaS", "industry", r"\bsaas\b"),
    Term("Education", "industry", r"\bedtech\b|higher education (?:clients|institutions|sector)|education (?:sector|industry|"
         r"clients|technology|market)|\bk-12\b", r"k-12 education|universit"),
    Term("Automotive", "industry", r"automotive|mobility|vehicles?\b|\boems?\b"),
    Term("Private equity", "industry", r"private equity|\bpe firms?\b|portfolio companies"),
    # ---- soft skills (scored; never added: a skills line of adjectives reads as keyword stuffing)
    Term("Communication", "soft", r"communication skills|communicat(?:e|ing) (?:clearly|effectively)|written and verbal|"
         r"oral communication|written communication|confident communication|explain your thinking",
         resume=r"communicat|\bpresent(?:ed|ations?)\b"),
    Term("Problem solving", "soft", r"problem[- ]solving|solve (?:complex )?problems", resume=r"problem|diagnos|solv"),
    Term("Leadership", "soft", r"\bleadership\b|\blead(?:ing)? (?:teams?|others)", resume=r"\bled\b|leadership|\blead\b"),
    Term("Collaboration", "soft", r"collaborat|team ?work|teams?\b", resume=r"collaborat|\bteams?\b|cross-functional"),
    Term("Attention to detail", "soft", r"attention to detail|detail[- ]oriented|pride in the details",
         resume=r"validat\w* every|detail"),
    Term("Analytical thinking", "soft", r"analytical (?:thinking|mindset)|critical thinking|structured thinking",
         resume=r"analy"),
    Term("Ownership", "soft", r"ownership|self[- ]starter|initiative|bias (?:toward|for) action",
         resume=r"\bowned\b|end-to-end|independently"),
    Term("Adaptability", "soft", r"ambiguity|fast-paced|adaptab"),
    Term("Curiosity", "soft", r"curio"),
]

# action verbs (base form -> resume past tense); the first word of a duty line is read against this list
_IRREGULAR = {"build": "built", "lead": "led", "drive": "drove", "write": "wrote", "run": "ran", "make": "made",
              "bring": "brought", "take": "took", "find": "found", "think": "thought", "teach": "taught",
              "get": "got", "give": "gave", "own": "owned", "plan": "planned", "ship": "shipped", "scope": "scoped",
              "co-operate": "cooperated", "cooperate": "cooperated", "prepare": "prepared", "deliver": "delivered",
              "set": "set", "spearhead": "spearheaded", "identify": "identified", "apply": "applied", "help": "helped",
              "map": "mapped", "audit": "audited", "pilot": "piloted", "partner": "partnered", "commit": "committed"}
ACTION_VERBS = set("""analyze assess build collaborate communicate conduct coordinate create define deliver design develop
drive evaluate execute facilitate gather generate identify implement improve influence launch lead manage measure model
monitor optimize own partner plan prepare present prioritize prototype research scope ship spearhead support synthesize
test translate write cooperate co-operate engage advise recommend investigate interview map frame structure solve
streamline transform validate visualize iterate align build establish track report champion craft envision""".split())


_NOT_A_VERB_AFTER = {"record", "records"}


def past(verb: str) -> str:
    v = verb.lower()
    if v in _IRREGULAR:
        return _IRREGULAR[v]
    if v.endswith("e"):
        return v + "d"
    if v.endswith("y") and v[-2:-1] not in "aeiou":
        return v[:-1] + "ied"
    return v + "ed"


# ----------------------------------------------------------------------------------------------- extraction

_KIND_WEIGHT = {"required": 1.5, "skills": 1.5, "responsibilities": 1.2, "preferred": 1.0, "summary": 0.8, "other": 0.7}
_BASE_WEIGHT = {"hard": 3.0, "industry": 2.0, "soft": 1.0}
_ACRONYM = re.compile(r"(?<![\w/])(?:[A-Z]{2,6}\d?|[A-Z]\d?/[A-Z]\d?|[A-Z][a-z]+[A-Z][A-Za-z]+)(?![\w/])")
_NOT_A_TOOL = set("""US USA UK EU EEO EOE PST EST CST MST PT ET HQ CEO CFO COO CTO VP SVP EVP PM APM APMs PMs MBA MS BA BS
PhD JD ID LLC LTD INC AM FAQ OR AND THE NOT USD CA NY IL MA TX WA DC GA NJ PA MI FL CO LA SF NYC II III IV
DEI LGBTQ ADA EEOC OFCCP GPA FT PTO OTE YoE ASAP ETA FYI TBD CPT OPT EAD TN GC STEM PST PDT AI LinkedIn GitHub YouTube
Glassdoor Indeed LLM LLMs SELF STATUS""".split())
# two-letter acronyms are mostly noise ("CS" for computer science, "LI"); a few are real skills
_TWO_LETTER_OK = {"BI", "ML", "QA", "UX", "UI", "HR", "PR", "VR", "AR", "GA", "PM"}
_LONG_ACRONYMS = {"HIPAA", "GAAP", "HEOR", "SPSS", "MATLAB", "JIRA", "HEDIS", "FHIR", "HL7", "SOX", "IFRS", "CFA", "PMP",
                  "CAPM", "OKRS", "KPIS", "SAAS", "PAAS", "IAAS", "ETL", "API", "APIS", "NLP", "RAG", "MLOPS"}
# shouted English words are not tools ("Solace Health will NEVER request bank details")
_SHOUTED = re.compile(r"^(?:NEVER|MUST|NOTE|PLEASE|ONLY|ALL|WILL|YOUR|WITH|THIS|THAT|JOB|ROLE|TEAM|APPLY|NOW|NOT|ANY|"
                      r"WHO|WHAT|WHY|HOW|OUR|YOU|ARE|FOR|AND|THE|NEW|JOIN|ABOUT|IMPORTANT|REQUIRED|PREFERRED|BONUS|"
                      r"FULL|TIME|REMOTE|HYBRID|ONSITE|ON|OFF|YES|NO|DO|DON|CAN|MAY|EVERY|EACH|FREE|TOP|BEST)$")


@dataclass
class Keyword:
    name: str
    kind: str  # hard | industry | soft | verb | title
    weight: float
    term: Term | None = None  # the vocabulary entry, when there is one
    verb: str = ""  # for kind == "verb": the base form

    def found_in(self, text: str) -> bool:
        if self.kind == "verb":
            return re.search(rf"\b{re.escape(past(self.verb))}\b|\b{re.escape(self.verb)}(?:s|ing)?\b", text, re.I) is not None
        if self.term is not None:
            return self.term.on_resume().search(text) is not None
        return re.search(rf"(?<![\w/]){re.escape(self.name)}(?![\w/])", text, re.I if self.kind != "hard" else 0) is not None


@dataclass
class Keywords:
    title: str
    items: list[Keyword] = field(default_factory=list)

    def by_kind(self, kind: str) -> list[Keyword]:
        return [k for k in self.items if k.kind == kind]

    def verbs(self) -> set[str]:
        return {k.verb for k in self.items if k.kind == "verb"}


def clean_title(title: str) -> str:
    """The role as a resume would mirror it: "[2027] Associate Product Manager, Early Career" -> "Associate Product
    Manager"; "Consultant - Life Sciences Advisory, Health Segment - Campus 2027" -> "Consultant, Life Sciences
    Advisory"."""
    t = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", title or "")
    t = re.sub(r"^\s*(?:job application for|application for|apply for|apply to|applying for)\s+", "", t, flags=re.I)  # Greenhouse page titles
    t = re.sub(r"\s+(?:@|at)\s+.*$", "", t)  # "... @ Solace", "... at Roblox"
    parts = [p.strip(" -–—,|:") for p in re.split(r"\s+[-–—|]\s+|,\s+|:\s+", t) if p.strip(" -–—,|:")]
    noise = re.compile(r"^(?:early careers?|new grad(?:uate)?s?|campus|class of|university|entry[- ]level|full[- ]time|"
                       r"intern(?:ship)?|remote|hybrid|on-?site|usa?|united states|americas|north america|emea|apac|"
                       r"[\w .]+, [A-Z]{2}|(?:spring|summer|fall|winter)?\s*20\d\d|20\d\d|mba|ms|undergrad(?:uate)?|"
                       r"multiple locations|various locations|[A-Z][a-z]+(?: [A-Z][a-z]+)?/[A-Z][a-z]+(?: [A-Z][a-z]+)?|"
                       r"health segment|naelfy\d+)\b.*$", re.I)
    keep = []
    parts = [re.sub(r"\s+(?:full|part)[- ]time\b", "", p, flags=re.I).strip() for p in parts]
    # "Product Management II" / "Product Manager 2": the role, without the level
    parts = [re.sub(r"\s+(?:I{1,3}|IV|[1-4])$", "", re.sub(r"\bProduct Management\b", "Product Manager", p)) for p in parts]
    parts = [re.sub(r"^(Product|UX|User Experience|Visual|Interaction) Design$", r"\1 Designer", p, flags=re.I) for p in parts]
    # a program's name next to the role ("Amazon Leadership Accelerator - Product Manager"): the role is the title
    if any(_ROLE_NOUN.search(p) for p in parts):
        parts = [p for p in parts if _ROLE_NOUN.search(p) or not _PROGRAM.search(p)]
    # "Associates and Consultants (2027 Graduates)" -> "Associate and Consultant"
    parts = [re.sub(r"\b(Associate|Consultant|Analyst|Manager|Engineer|Scientist|Designer|Researcher|Strategist|Specialist)s\b",
                    r"\1", p) for p in parts]
    for p in parts:
        p = re.sub(r"\b(?:campus|early career|new grad)\b\s*20\d\d|\b20\d\d\b|\b(?:usa|us)\b", " ", p, flags=re.I)
        p = re.sub(r"\s{2,}", " ", p).strip(" -–—,")
        # a page title's site part ("Apply for Consultant - Americas | Application"), not "Application Developer"
        if p and not noise.match(p) and not re.fullmatch(r"(?:job )?application(?: form)?|careers?|jobs?|apply", p, re.I):
            keep.append(p)
    return ", ".join(keep[:2]) if keep else (title or "").strip()


def extract_keywords(job: CleanJob, title: str = "") -> Keywords:
    """Step 1: the posting's keywords, each weighted by kind (hard > industry > soft) and by where it is asked for
    (required > responsibilities > preferred > the rest); boilerplate is never read."""
    best: dict[str, Keyword] = {}

    def add(name: str, kind: str, w: float, term: Term | None = None, verb: str = "") -> None:
        key = (kind + ":" + name).lower()
        if key not in best or best[key].weight < w:
            best[key] = Keyword(name, kind, w, term, verb)

    blocks = [(b.kind, b.text) for b in job.blocks if b.kind != "boilerplate"]
    # When the posting has its own sections, skills come from them: a company blurb ("a $1 billion valuation") is not a
    # skill the role asks for. Industry terms come from anywhere ("Healthcare in the U.S. is broken").
    structured = any(k in ("required", "preferred", "responsibilities", "skills") for k, _ in blocks)
    if title:
        blocks.append(("required", title))
    for kind, text in blocks:
        mult = _KIND_WEIGHT.get(kind, 0.7)
        for t in VOCAB:
            if structured and kind in ("other", "summary") and t.kind != "industry":
                continue
            hits = len(t.rx().findall(text))
            if hits:
                add(t.name, t.kind, _BASE_WEIGHT[t.kind] * mult * (1.0 + 0.15 * min(hits - 1, 3)), t)
        if kind in ("required", "preferred", "skills", "responsibilities"):
            for m in _ACRONYM.finditer(text):
                tok = m.group(0)
                if tok in _NOT_A_TOOL or _SHOUTED.match(tok) or any(t.rx().fullmatch(tok) for t in VOCAB):
                    continue
                if len(tok) == 2 and tok not in _TWO_LETTER_OK:
                    continue
                if tok.isupper() and len(tok) >= 4 and not re.search(r"\d", tok) and tok not in _LONG_ACRONYMS:
                    continue  # a shouted word, not a tool ("STATUS"); real ones are listed
                if title and re.search(rf"\b{re.escape(tok)}\b", title):
                    continue  # the company or program name in the title ("GTM Strategy & Operations")
                add(tok, "hard", 1.5 * mult)
        if kind in ("responsibilities", "required", "other", "summary"):
            first = re.match(r"^[\W\d]*([A-Za-z-]+)(?:\s+([A-Za-z-]+))?", text)
            # "Track record of ..." is not the verb track (Amazon, 2027 ALA posting)
            if first and first.group(1).lower() in ACTION_VERBS and (first.group(2) or "").lower() not in _NOT_A_VERB_AFTER:
                v = first.group(1).lower()
                add(v.capitalize(), "verb", 0.6 * mult, verb=v)
            for v in re.findall(r"(?:^|,|;|\band\b)\s*([a-z-]+)\b", text):  # "gather feedback, and analyze data"
                if v in ACTION_VERBS:
                    add(v.capitalize(), "verb", 0.4 * mult, verb=v)
    items = sorted(best.values(), key=lambda k: -k.weight)
    # a posting names a handful of verbs that matter; the long tail is noise
    verbs = [k for k in items if k.kind == "verb"][:10]
    items = [k for k in items if k.kind != "verb"] + verbs
    ct = clean_title(title)
    if ct:
        items.append(Keyword(ct, "title", 4.0))
    return Keywords(ct, items)


# ----------------------------------------------------------------------------------------------- scoring

@dataclass
class Score:
    percent: int
    matched: list[str]
    missing: list[str]
    missing_hard: list[str]  # the hard skills and industry terms not on the resume, most important first

    def short(self) -> str:
        return f"{self.percent}%"


def _title_hit(title: str, text: str) -> float:
    """1.0 when the resume says the role's title ("Associate Product Manager"; "Automotive Consultant" for "Consultant,
    Automotive"), otherwise partial credit for its words."""
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z&-]+", title) if w.lower() not in {"and", "of", "the", "for", "&", "in"}]
    if not words:
        return 0.0
    if re.search(re.escape(title), text, re.I):
        return 1.0
    for line in text.splitlines():  # every word of the title in one line (the summary mirrors it)
        if all(re.search(rf"\b{re.escape(w)}\b", line, re.I) for w in words):
            return 1.0
    return 0.5 * sum(1 for w in words if re.search(rf"\b{re.escape(w)}\b", text, re.I)) / len(words)


_PROGRAM = re.compile(r"\b(?:accelerator|program(?:me)?|rotation(?:al)?|leadership development|apprenticeship|fellowship|"
                      r"university graduate|early career|new grad)\b", re.I)
_ROLE_NOUN = re.compile(r"\b(?:consultant|associate|analyst|manager|designer|researcher|engineer|scientist|specialist|"
                        r"coordinator|lead|director|strategist|advisor|architect|intern|fellow|officer|owner)s?\b", re.I)


def headline_title(title: str) -> str:
    """The cleaned title as a person would write it in a sentence: "Consultant, Automotive" -> "Automotive Consultant";
    "Consultant, Life Sciences Advisory" -> "Life Sciences Advisory Consultant"."""
    parts = [p.strip() for p in title.split(", ") if p.strip()]
    if len(parts) < 2:
        return title
    role = next((p for p in parts if _ROLE_NOUN.search(p)), parts[0])
    other = next((p for p in parts if p is not role), "")
    if other and len(other.split()) <= 3 and all(w[:1].isupper() for w in other.split()) and not _ROLE_NOUN.search(other):
        return f"{other} {role}"
    return role


def score(text: str, kw: Keywords) -> Score:
    """Step 3: the share of the posting's keyword weight the resume text shows (Jobscan-style: hard skills count most,
    then the job title, industry terms, soft skills and verbs)."""
    total = got = 0.0
    matched, missing, missing_hard = [], [], []
    for k in kw.items:
        total += k.weight
        if k.kind == "title":
            hit = _title_hit(k.name, text)
            got += k.weight * hit
            (matched if hit >= 1.0 else missing).append(f"job title '{k.name}'")
            continue
        if k.found_in(text):
            got += k.weight
            matched.append(k.name)
        else:
            missing.append(k.name)
            if k.kind in ("hard", "industry"):
                missing_hard.append(k.name)
    return Score(round(100 * got / total) if total else 0, matched, missing, missing_hard)


def resume_text(resume) -> str:
    """Everything a resume says, as one text (what an ATS reads)."""
    parts = [resume.summary or ""]
    for s in resume.sections:
        parts.append(s.heading)
        for e in s.entries:
            parts.append(e.title)
            for r in e.roles:
                parts += [r.title] + list(r.bullets)
        for ln in s.lines:
            parts += [ln.label, ln.text]
    return "\n".join(p.replace("*", "") for p in parts)

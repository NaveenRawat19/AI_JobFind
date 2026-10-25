"""Count which skills job postings ask for. No LLM: a fixed vocabulary plus regexes,
so the same postings always give the same numbers and every count can be traced.

Short or common-word names (Go, Swift, React, RAG) are matched case-sensitively,
otherwise "go to market", "swift response" and "react to incidents" would count.
A posting counts a skill once however often it repeats it.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class Skill:
    name: str
    category: str
    patterns: tuple[str, ...]
    exact_case: bool = False

    def regex(self) -> re.Pattern:
        body = "|".join(self.patterns)
        return re.compile(body, 0 if self.exact_case else re.I)


def _s(name: str, category: str, *patterns: str, exact_case: bool = False) -> Skill:
    return Skill(name, category, patterns or (rf"\b{re.escape(name)}\b",), exact_case)


# Boundaries that survive symbols: \b fails next to "+", "#" and ".".
_L, _R = r"(?<![\w.+#])", r"(?![\w+#])"

SKILLS: tuple[Skill, ...] = (
    # languages
    _s("Python", "language"),
    _s("Java", "language", r"\bJava\b(?!\s*Script)"),
    _s("JavaScript", "language", r"\bJava\s?Script\b", r"\bECMAScript\b"),
    _s("TypeScript", "language", r"\bType\s?Script\b"),
    _s("Go", "language", r"\bGo\b(?!\s+(?:to|live|beyond|forward|above)\b)", r"(?i:\bGolang\b)", exact_case=True),
    _s("C++", "language", rf"{_L}C\+\+{_R}"),
    _s("C#", "language", rf"{_L}C#{_R}"),
    _s("Rust", "language", r"\bRust\b", exact_case=True),
    _s("Kotlin", "language"),
    _s("Swift", "language", r"\bSwift\b(?!\s+(?:response|action|delivery))", exact_case=True),
    _s("Scala", "language"),
    _s("Ruby", "language", r"\bRuby\b"),
    _s("PHP", "language", r"\bPHP\b"),
    _s("SQL", "language", r"\bSQL\b", r"\bT-?SQL\b", r"\bPL/?SQL\b"),
    # backend / frameworks
    _s("Node.js", "backend", r"\bNode\.?js\b"),
    _s("Django", "backend"),
    _s("Flask", "backend"),
    _s("FastAPI", "backend", r"\bFast\s?API\b"),
    _s("Spring Boot", "backend", r"\bSpring\s?Boot\b", r"\bSpring\s+(?:Framework|MVC|Cloud)\b"),
    _s(".NET", "backend", rf"{_L}\.NET\b", r"\bASP\.NET\b", r"\bdotnet\b"),
    _s("Express", "backend", r"\bExpress(?:\.js|JS)\b", r"\bExpress\s+(?:framework|server)\b"),
    _s("NestJS", "backend", r"\bNest\.?JS\b"),
    _s("GraphQL", "backend"),
    _s("REST APIs", "backend", r"\bREST(?:ful)?\b", r"(?i:\brestful\b)", exact_case=True),
    _s("Microservices", "backend", r"\bmicro-?services?\b"),
    _s("gRPC", "backend"),
    # frontend / mobile
    _s("React", "frontend", r"\bReact(?:\.?js|JS)?\b(?!\s+Native)", exact_case=True),
    _s("Angular", "frontend", r"\bAngular(?:JS)?\b", exact_case=True),
    _s("Vue", "frontend", r"\bVue(?:\.?js)?\b", exact_case=True),
    _s("Next.js", "frontend", r"\bNext\.?js\b"),
    _s("HTML/CSS", "frontend", r"\bHTML5?\b", r"\bCSS3?\b"),
    _s("Redux", "frontend", r"\bRedux\b"),
    _s("React Native", "mobile", r"\bReact\s+Native\b"),
    _s("Android", "mobile", r"\bAndroid\b"),
    _s("iOS", "mobile", r"\biOS\b", exact_case=True),
    _s("Flutter", "mobile"),
    # cloud / devops
    _s("AWS", "cloud", r"\bAWS\b", r"\bAmazon Web Services\b"),
    _s("Azure", "cloud", r"\bAzure\b"),
    _s("GCP", "cloud", r"\bGCP\b", r"\bGoogle Cloud\b"),
    _s("Docker", "cloud"),
    _s("Kubernetes", "cloud", r"\bKubernetes\b", r"\bK8s\b"),
    _s("Terraform", "cloud"),
    _s("CI/CD", "cloud", r"\bCI\s?/\s?CD\b", r"\bcontinuous (?:integration|delivery|deployment)\b"),
    _s("Jenkins", "cloud"),
    _s("GitHub Actions", "cloud", r"\bGitHub Actions\b"),
    _s("Linux", "cloud"),
    _s("Serverless", "cloud", r"\bserverless\b", r"\bAWS Lambda\b"),
    _s("Observability", "cloud", r"\bobservability\b", r"\bPrometheus\b", r"\bGrafana\b", r"\bDatadog\b"),
    # data
    _s("PostgreSQL", "data", r"\bPostgres(?:QL)?\b"),
    _s("MySQL", "data"),
    _s("MongoDB", "data", r"\bMongo(?:DB)?\b"),
    _s("Redis", "data"),
    _s("Elasticsearch", "data", r"\bElastic\s?search\b", r"\bOpenSearch\b"),
    _s("Kafka", "data"),
    _s("Spark", "data", r"\b(?:Py)?Spark\b", exact_case=True),
    _s("Airflow", "data"),
    _s("Snowflake", "data"),
    _s("dbt", "data", r"\bdbt\b", exact_case=True),
    _s("Pandas", "data", r"\bPandas\b", exact_case=True),
    _s("ETL", "data", r"\bETL\b", r"\bELT\b", r"\bdata pipelines?\b"),
    _s("NoSQL", "data", r"\bNo-?SQL\b"),
    # AI / ML
    _s("LLMs", "ai", r"\bLLMs?\b", r"\blarge language models?\b", r"\bGPT-?\d?\b", r"\bgenerative AI\b", r"\bGenAI\b"),
    _s("RAG", "ai", r"\bRAG\b", r"(?i:\bretrieval[- ]augmented\b)", exact_case=True),
    _s("Prompt Engineering", "ai", r"\bprompt (?:engineering|design)\b", r"\bprompt(?:ing)? techniques\b"),
    _s("Vector Databases", "ai", r"\bvector (?:databases?|stores?|search|DBs?)\b", r"\bPinecone\b", r"\bWeaviate\b", r"\bMilvus\b", r"\bChroma(?:DB)?\b", r"\bFAISS\b", r"\bpgvector\b"),
    _s("Fine-tuning", "ai", r"\bfine[- ]?tun(?:e|ing|ed)\b", r"\bLoRA\b", r"\bRLHF\b"),
    _s("PyTorch", "ai", r"\bPy\s?Torch\b"),
    _s("TensorFlow", "ai", r"\bTensorFlow\b"),
    _s("scikit-learn", "ai", r"\bscikit-?learn\b", r"\bsklearn\b"),
    _s("Machine Learning", "ai", r"\bmachine learning\b", r"\bML\b", exact_case=False),
    _s("Deep Learning", "ai", r"\bdeep learning\b"),
    _s("NLP", "ai", r"\bNLP\b", r"\bnatural language processing\b"),
    _s("LangChain", "ai", r"\bLang(?:Chain|Graph)\b", r"\bLlamaIndex\b"),
    _s("Hugging Face", "ai", r"\bHugging\s?Face\b"),
    _s("MLOps", "ai", r"\bMLOps\b", r"\bML\s?Ops\b", r"\bMLflow\b", r"\bKubeflow\b"),
    _s("AI Agents", "ai", r"\bAI agents?\b", r"\bagentic\b", r"\bmulti-agent\b"),
    # practices
    _s("Git", "practice", r"\bGit\b", exact_case=True),
    _s("Agile", "practice", r"\bAgile\b", r"\bScrum\b"),
    _s("System Design", "practice", r"\bsystem(?:s)? design\b", r"\bdistributed systems?\b"),
    _s("Testing", "practice", r"\bunit tests?\b", r"\btest[- ]driven\b", r"\bTDD\b", r"\bautomated testing\b", r"\bJest\b", r"\bPytest\b"),
    _s("Data Structures & Algorithms", "practice", r"\bdata structures\b"),
)

CATEGORIES = sorted({s.category for s in SKILLS})
_COMPILED = [(s, s.regex()) for s in SKILLS]
BY_NAME = {s.name: s for s in SKILLS}

# Checked in order; the first match wins, so the specific families come first.
ROLE_RULES: tuple[tuple[str, re.Pattern], ...] = tuple(
    (name, re.compile(pat, re.I)) for name, pat in (
        ("ai-ml", r"\b(?:ai|a\.i\.|ml|llm|genai|gen ai|generative|nlp|machine learning|deep learning|"
                  r"data scien\w+|applied scien\w+|research engineer|computer vision)\b"),
        ("mobile", r"\b(?:android|ios|mobile|react native|flutter)\b"),
        ("data", r"\b(?:data engineer\w*|data analyst|analytics|etl|big data|data platform)\b"),
        ("devops-sre", r"\b(?:devops|sre|site reliability|platform engineer\w*|infrastructure|cloud engineer\w*|"
                       r"systems engineer\w*|release engineer\w*)\b"),
        ("full-stack", r"\bfull[- ]?stack\b"),
        ("frontend", r"\b(?:front[- ]?end|ui engineer|ui developer|web developer|web engineer)\b"),
        ("backend", r"\b(?:back[- ]?end|server[- ]side|api engineer|java developer|python developer)\b"),
        ("software-engineer", r"\b(?:software|application|developer|programmer|sde|swe|engineer)\b"),
    ))


def role_of(title: str) -> str:
    for name, rx in ROLE_RULES:
        if rx.search(title or ""):
            return name
    return "other"


ROLES = [name for name, _ in ROLE_RULES] + ["other"]


def extract(text: str) -> set[str]:
    """Names of the vocabulary skills mentioned in `text`."""
    return {s.name for s, rx in _COMPILED if rx.search(text)} if text else set()


def count_skills(postings: Iterable[str]) -> tuple[int, Counter]:
    """(number of postings, how many postings mention each skill)."""
    counts: Counter = Counter()
    n = 0
    for text in postings:
        n += 1
        counts.update(extract(text))
    return n, counts

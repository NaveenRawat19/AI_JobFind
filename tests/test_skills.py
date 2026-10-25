"""Skill extraction: what counts, and, more importantly, what must not."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobhunt import skills


@pytest.mark.parametrize("text, expected", [
    ("Build RAG pipelines on vector databases", {"RAG", "Vector Databases"}),
    ("retrieval-augmented generation with Pinecone", {"RAG", "Vector Databases"}),
    ("Experience with LLMs and prompt engineering", {"LLMs", "Prompt Engineering"}),
    ("large language models, GenAI", {"LLMs"}),
    ("Fine-tuning open models with LoRA", {"Fine-tuning"}),
    ("PyTorch or TensorFlow", {"PyTorch", "TensorFlow"}),
    ("Strong C++ and C# skills", {"C++", "C#"}),
    ("ASP.NET and .NET Core", {".NET"}),
    ("Node.js, Nodejs, NodeJS", {"Node.js"}),
    ("Kubernetes (k8s) on AWS", {"Kubernetes", "AWS"}),
    ("We use Go and Golang", {"Go"}),
    ("TypeScript/JavaScript, React", {"TypeScript", "JavaScript", "React"}),
    ("Postgres, MySQL and Redis", {"PostgreSQL", "MySQL", "Redis"}),
    ("CI/CD with GitHub Actions", {"CI/CD", "GitHub Actions"}),
    ("RESTful services and GraphQL", {"REST APIs", "GraphQL"}),
])
def test_finds_the_skill(text, expected):
    assert expected <= skills.extract(text)


@pytest.mark.parametrize("text, absent", [
    ("Java developer", "JavaScript"),
    ("JavaScript developer", "Java"),
    ("Java Script and Java", None),
    ("a go-to-market role, ready to go to production", "Go"),
    ("Let's go!", "Go"),
    ("react quickly to incidents", "React"),
    ("React Native apps", "React"),
    ("you will rest assured and rest of the team", "REST APIs"),
    ("a rag cloth", "RAG"),
    ("swift response times", "Swift"),
    ("the Rust belt", None),
    ("understand the Git history", "GitHub Actions"),
    ("Spring is the busiest season", "Spring Boot"),
    ("", "Python"),
])
def test_does_not_over_match(text, absent):
    if absent:
        assert absent not in skills.extract(text)


def test_java_vs_javascript_are_distinct():
    assert skills.extract("Java Spring Boot") >= {"Java", "Spring Boot"}
    assert "Java" not in skills.extract("JavaScript only")
    assert "JavaScript" not in skills.extract("Java only")


def test_a_posting_counts_a_skill_once_however_often_it_repeats():
    n, counts = skills.count_skills(["Python Python Python", "python", "Rust"])
    assert n == 3 and counts["Python"] == 2 and counts["Rust"] == 1


def test_symbols_do_not_break_boundaries():
    assert "C++" in skills.extract("Skills: C++, Rust")
    assert "C++" in skills.extract("(C++)")
    assert "C#" in skills.extract("C#/.NET")


def test_vocabulary_is_unique_and_compiles():
    names = [s.name for s in skills.SKILLS]
    assert len(names) == len(set(names))
    assert set(skills.CATEGORIES) >= {"ai", "language", "cloud", "data"}


@pytest.mark.parametrize("title, role", [
    ("Senior AI Engineer", "ai-ml"),
    ("Machine Learning Engineer II", "ai-ml"),
    ("LLM Platform Engineer", "ai-ml"),          # AI wins over platform
    ("Data Scientist", "ai-ml"),
    ("Android Developer", "mobile"),
    ("Data Engineer", "data"),
    ("Site Reliability Engineer", "devops-sre"),
    ("Full Stack Developer", "full-stack"),
    ("Frontend Engineer", "frontend"),
    ("Backend Engineer", "backend"),
    ("Software Engineer", "software-engineer"),
    ("Product Designer", "other"),
    ("", "other"),
])
def test_role_families(title, role):
    assert skills.role_of(title) == role


def test_ai_is_matched_as_a_word_not_a_substring():
    assert skills.role_of("Retail Engineer") != "ai-ml"
    assert skills.role_of("Maintenance Technician") == "other"

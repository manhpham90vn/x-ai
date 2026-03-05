"""Greeting utilities for x-ai CLI.

Provides randomized international greetings for the interactive mode.
"""

from __future__ import annotations

import random

# International greetings with language codes and names
GREETINGS = [
    # English
    ("Hello", "en"),
    ("Welcome", "en"),
    # Vietnamese
    ("Xin chào", "vi"),
    # Spanish
    ("Hola", "es"),
    ("Bienvenido", "es"),
    # French
    ("Bonjour", "fr"),
    ("Bienvenue", "fr"),
    # Japanese
    ("こんにちは", "ja"),
    ("ようこそ", "ja"),
    # German
    ("Hallo", "de"),
    ("Willkommen", "de"),
    # Italian
    ("Ciao", "it"),
    ("Benvenuto", "it"),
    # Portuguese
    ("Olá", "pt"),
    ("Bem-vindo", "pt"),
    # Korean
    ("안녕하세요", "ko"),
    # Chinese
    ("你好", "zh"),
    ("欢迎", "zh"),
    # Russian
    ("Привет", "ru"),
    ("Добро пожаловать", "ru"),
    # Arabic
    ("مرحبا", "ar"),
    # Hindi
    ("नमस्ते", "hi"),
    ("स्वागत है", "hi"),
    # Dutch
    ("Hallo", "nl"),
    ("Welkom", "nl"),
    # Swedish
    ("Hej", "sv"),
    ("Välkommen", "sv"),
]


def get_random_greeting() -> str:
    """Return a random greeting from the predefined list.

    Returns:
        A randomly selected greeting string in one of several languages.
    """
    greeting, _ = random.choice(GREETINGS)
    return greeting


def format_greeting(name: str | None = None) -> str:
    """Format a greeting with optional username.

    Args:
        name: Optional username to include in the greeting.

    Returns:
        A formatted greeting string, optionally including the username.
    """
    greeting = get_random_greeting()

    if name:
        return f"{greeting}, {name}!"
    return f"{greeting}!"


def get_greeting_with_context(context: str | None = None) -> str:
    """Get a greeting with additional context text.

    Args:
        context: Optional context to append (e.g., "interactive mode").

    Returns:
        A greeting string with context if provided.
    """
    greeting = format_greeting()

    if context:
        return f"{greeting.rstrip('!')} to x-ai {context}."
    return greeting

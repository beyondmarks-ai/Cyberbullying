import asyncio
import json
import sys

from azure_moderation import AzureModerator
from comment_monitor import load_dotenv

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


CASES = [
    ("Kannada abuse", "ನೀನು ಮೂರ್ಖ, ಯಾರಿಗೂ ನೀನು ಇಷ್ಟವಿಲ್ಲ."),
    ("Hinglish abuse", "Tu bilkul बेकार hai, sab tujhse nafrat karte hain."),
    ("Hindi family-targeted abuse", "नहीं ऐसी कोई। तेरी माँ की चूत। किस बात पे मारा है?"),
    ("Kannada-English mix", "ನೀನು totally useless, ಎಲ್ಲರೂ ನಿನ್ನನ್ನು hate ಮಾಡುತ್ತಾರೆ."),
    ("Benign Kannada", "ನಿಮ್ಮ ಕೆಲಸ ತುಂಬಾ ಚೆನ್ನಾಗಿದೆ, ಅಭಿನಂದನೆಗಳು!"),
]


async def main():
    load_dotenv()
    moderator = AzureModerator()
    try:
        for name, text in CASES:
            result = await moderator.analyze(text)
            print(json.dumps({"case": name, **result}, ensure_ascii=False))
    finally:
        await moderator.http.aclose()


if __name__ == "__main__":
    asyncio.run(main())

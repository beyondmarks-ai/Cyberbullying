import asyncio
import json
import sys

from azure_moderation import AzureModerator
from comment_monitor import load_dotenv

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


CASES = [
    ("Kannada abuse", "ನೀನು ಮೂರ್ಖ, ಯಾರಿಗೂ ನೀನು ಇಷ್ಟವಿಲ್ಲ.", "bullying"),
    ("Kanglish abuse", "Neenu moorkha, groupinda horage hogu.", "bullying"),
    ("Hinglish abuse", "Tu bilkul बेकार hai, sab tujhse nafrat karte hain.", "bullying"),
    ("Hindi family-targeted abuse", "तेरी माँ की चूत। किस बात पे मारा है?", "bullying"),
    ("Kannada-English mix", "ನೀನು totally useless, ಎಲ್ಲರೂ ನಿನ್ನನ್ನು hate ಮಾಡುತ್ತಾರೆ.", "bullying"),
    ("Tamil abuse", "நீ ஒரு முட்டாள். குழுவை விட்டு வெளியே போ.", "bullying"),
    ("Telugu abuse", "నువ్వు మూర్ఖుడివి. ఎవరూ నిన్ను ఇష్టపడరు.", "bullying"),
    ("Spanish abuse", "Eres inútil. Nadie te quiere en este grupo.", "bullying"),
    ("Arabic abuse", "أنت عديم الفائدة، لا أحد يريدك في مجموعتنا.", "bullying"),
    ("Benign Kannada", "ನಿಮ್ಮ ಕೆಲಸ ತುಂಬಾ ಚೆನ್ನಾಗಿದೆ, ಅಭಿನಂದನೆಗಳು!", "clear"),
    ("Reporting abuse", 'Someone called me "worthless". Please help me report the bullying.', "safe"),
]


async def main():
    load_dotenv()
    moderator = AzureModerator()
    passed = 0
    try:
        for name, text, expected in CASES:
            result = await moderator.analyze(text)
            ok = result["status"] in {"clear", "review"} if expected == "safe" else result["status"] == expected
            passed += ok
            print(json.dumps({"case": name, "expected": expected, "pass": ok, **result}, ensure_ascii=False))
    finally:
        await moderator.http.aclose()
    print(json.dumps({"passed": passed, "total": len(CASES)}))
    return passed == len(CASES)


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(main()) else 1)

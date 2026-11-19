"""Export readable CI Markdown reports as paginated PDF documents."""

import html
import sys
from pathlib import Path

import reportlab
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer


def main():
    source = Path(sys.argv[1])
    fonts = Path(reportlab.__file__).parent / "fonts"
    pdfmetrics.registerFont(TTFont("Report", str(fonts / "Vera.ttf")))
    styles = getSampleStyleSheet()
    style = styles["BodyText"]
    style.fontName = "Report"
    style.fontSize = 9
    style.leading = 13
    story = []
    for line in source.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            story.append(Spacer(1, 8))
        else:
            story.append(Paragraph(html.escape(line), style))
    SimpleDocTemplate(str(source.with_suffix(".pdf")), title=source.stem).build(story)


if __name__ == "__main__":
    main()

import zipfile, xml.etree.ElementTree as ET, sys

path = r"C:\Users\ebane\Downloads\acumen_automation_proposal.docx"
with zipfile.ZipFile(path) as z:
    with z.open("word/document.xml") as f:
        tree = ET.parse(f)

ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
for para in tree.iter(ns + "p"):
    text = "".join(t.text or "" for t in para.iter(ns + "t"))
    if text.strip():
        print(text)

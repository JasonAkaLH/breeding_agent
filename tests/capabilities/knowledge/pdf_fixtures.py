"""Small, public synthetic PDFs; no PDF writer dependency for the basic fixture."""


def text_pdf(*pages: str | None) -> bytes:
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"", b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for text in pages:
        number = len(objects) + 1
        kids.append(f"{number} 0 R")
        content = b"" if text is None else b"BT /F1 12 Tf 20 700 Td (" + text.encode("ascii").replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)") + b") Tj ET"
        objects.extend([
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 800] /Resources << /Font << /F1 3 0 R >> >> /Contents {number + 1} 0 R >>".encode(),
            f"<< /Length {len(content)} >>\nstream\n".encode() + content + b"\nendstream",
        ])
    objects[1] = f"<< /Type /Pages /Count {len(pages)} /Kids [{' '.join(kids)}] >>".encode()
    raw = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(raw))
        raw += f"{number} 0 obj\n".encode() + obj + b"\nendobj\n"
    start = len(raw)
    raw += f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode()
    raw += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:])
    return raw + f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n".encode()


def unreadable_page_pdf(*, mixed=False, encrypted=False) -> bytes:
    from io import BytesIO

    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import DecodedStreamObject, NameObject

    writer = PdfWriter()
    if mixed:
        writer.add_page(PdfReader(BytesIO(text_pdf("Readable first page"))).pages[0])
    page = writer.add_blank_page(width=600, height=800)
    stream = DecodedStreamObject()
    stream.set_data(b"q 100 0 0 100 0 0 cm BI /W 1 /H 1 /CS /RGB /BPC 8 ID \xff\xff\xff EI Q")
    page[NameObject("/Contents")] = stream
    if encrypted:
        writer.encrypt("private-password")
    result = BytesIO()
    writer.write(result)
    return result.getvalue()

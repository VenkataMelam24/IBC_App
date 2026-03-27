PDF_PAGE_WIDTH = 595
PDF_PAGE_HEIGHT = 842
PDF_LEFT = 50
PDF_RIGHT = 545
PDF_BOTTOM = 70


def _escape_pdf_text(value):
    text = str(value)
    text = text.encode("latin-1", "replace").decode("latin-1")
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _truncate_text(value, limit):
    text = str(value)
    if len(text) <= limit:
        return text
    return f"{text[:limit - 3]}..."


def build_purchase_order_pdf(purchase_order):
    items = list(purchase_order.items.select_related("product").all())
    pages = []
    commands = []
    y_position = PDF_PAGE_HEIGHT - 50

    def add_text(x, y, text, size=12):
        escaped = _escape_pdf_text(text)
        commands.append(
            f"BT /F1 {size} Tf 1 0 0 1 {x} {y} Tm ({escaped}) Tj ET"
        )

    def add_line(y):
        commands.append(f"{PDF_LEFT} {y} m {PDF_RIGHT} {y} l S")

    def start_page(continued=False):
        nonlocal commands, y_position
        if commands:
            pages.append("\n".join(commands))
        commands = ["0.5 w"]
        y_position = PDF_PAGE_HEIGHT - 50

        add_text(PDF_LEFT, y_position, "IBC", size=20)
        y_position -= 28
        title = "Purchase Order" if not continued else "Purchase Order (continued)"
        add_text(PDF_LEFT, y_position, title, size=16)
        y_position -= 24

        if not continued:
            add_text(PDF_LEFT, y_position, f"PO Number: {purchase_order.po_number}")
            add_text(330, y_position, f"Date: {purchase_order.created_at:%Y-%m-%d}")
            y_position -= 18
            add_text(PDF_LEFT, y_position, f"Vendor: {purchase_order.vendor.name}")
            add_text(330, y_position, f"Ordered By: {purchase_order.created_by}")
            y_position -= 28

        add_line(y_position + 6)
        add_text(PDF_LEFT, y_position - 8, "S.No")
        add_text(120, y_position - 8, "Product")
        add_text(430, y_position - 8, "Quantity")
        y_position -= 24
        add_line(y_position + 8)

    def ensure_space(required_height):
        if y_position - required_height < PDF_BOTTOM:
            start_page(continued=True)

    start_page()

    for index, item in enumerate(items, start=1):
        ensure_space(26)
        add_text(PDF_LEFT, y_position - 8, str(index), size=11)
        add_text(120, y_position - 8, _truncate_text(item.product.display_name, 42), size=11)
        add_text(435, y_position - 8, str(item.quantity), size=11)
        y_position -= 24
        add_line(y_position + 8)

    if commands:
        pages.append("\n".join(commands))

    objects = {}
    objects[1] = b"<< /Type /Catalog /Pages 2 0 R >>"

    kids = []
    page_count = len(pages)
    for index in range(page_count):
        page_object_number = 4 + (index * 2)
        kids.append(f"{page_object_number} 0 R")
    objects[2] = (
        f"<< /Type /Pages /Count {page_count} /Kids [{' '.join(kids)}] >>"
    ).encode("latin-1")
    objects[3] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"

    for index, page_content in enumerate(pages):
        page_object_number = 4 + (index * 2)
        content_object_number = page_object_number + 1
        objects[page_object_number] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PDF_PAGE_WIDTH} {PDF_PAGE_HEIGHT}] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_object_number} 0 R >>"
        ).encode("latin-1")
        content_bytes = page_content.encode("latin-1", "replace")
        objects[content_object_number] = (
            f"<< /Length {len(content_bytes)} >>\n".encode("latin-1")
            + b"stream\n"
            + content_bytes
            + b"\nendstream"
        )

    max_object_number = max(objects)
    pdf_bytes = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]

    for object_number in range(1, max_object_number + 1):
        offsets.append(len(pdf_bytes))
        pdf_bytes.extend(f"{object_number} 0 obj\n".encode("latin-1"))
        pdf_bytes.extend(objects[object_number])
        pdf_bytes.extend(b"\nendobj\n")

    xref_offset = len(pdf_bytes)
    pdf_bytes.extend(f"xref\n0 {max_object_number + 1}\n".encode("latin-1"))
    pdf_bytes.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        pdf_bytes.extend(f"{offset:010d} 00000 n \n".encode("latin-1"))

    pdf_bytes.extend(
        (
            f"trailer\n<< /Size {max_object_number + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF"
        ).encode("latin-1")
    )

    return bytes(pdf_bytes)

def normalize_product_name(value):
    return " ".join((value or "").strip().lower().split())


def get_normalized_product_names(product):
    normalized_names = set()
    names = [product.product_name, product.display_name]
    aliases = product.aliases.all()

    for alias in aliases:
        names.append(alias.alias_name)

    for name in names:
        normalized_name = normalize_product_name(name)
        if normalized_name:
            normalized_names.add(normalized_name)

    return normalized_names


def product_matches_name(product, value):
    normalized_value = normalize_product_name(value)
    if not normalized_value:
        return False

    return normalized_value in get_normalized_product_names(product)

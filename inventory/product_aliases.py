from .models import ProductAlias
from .product_matching import normalize_product_name


def clean_product_aliases(alias_values):
    cleaned_aliases = []
    seen_aliases = set()

    for alias_value in alias_values:
        cleaned_value = " ".join((alias_value or "").split()).strip()
        normalized_value = normalize_product_name(cleaned_value)

        if not normalized_value or normalized_value in seen_aliases:
            continue

        seen_aliases.add(normalized_value)
        cleaned_aliases.append(cleaned_value)

    return cleaned_aliases


def sync_product_aliases(product, alias_values):
    cleaned_aliases = clean_product_aliases(alias_values)
    desired_aliases = {
        normalize_product_name(alias_name): alias_name
        for alias_name in cleaned_aliases
    }
    existing_aliases = {
        normalize_product_name(alias.alias_name): alias
        for alias in product.aliases.all()
    }

    aliases_to_create = []
    aliases_to_update = []
    aliases_to_delete = []

    for normalized_name, existing_alias in existing_aliases.items():
        alias_name = desired_aliases.get(normalized_name)
        if alias_name is None:
            aliases_to_delete.append(existing_alias.pk)
            continue

        if existing_alias.alias_name != alias_name:
            existing_alias.alias_name = alias_name
            aliases_to_update.append(existing_alias)

    for normalized_name, alias_name in desired_aliases.items():
        if normalized_name not in existing_aliases:
            aliases_to_create.append(ProductAlias(product=product, alias_name=alias_name))

    if aliases_to_delete:
        product.aliases.filter(pk__in=aliases_to_delete).delete()

    if aliases_to_create:
        ProductAlias.objects.bulk_create(aliases_to_create)

    if aliases_to_update:
        ProductAlias.objects.bulk_update(aliases_to_update, ["alias_name"])


def merge_product_aliases(product, alias_values):
    desired_aliases = {
        normalize_product_name(alias_name): alias_name
        for alias_name in clean_product_aliases(alias_values)
    }
    existing_aliases = {
        normalize_product_name(alias.alias_name): alias
        for alias in product.aliases.all()
    }

    aliases_to_create = []
    aliases_to_update = []

    for normalized_name, alias_name in desired_aliases.items():
        existing_alias = existing_aliases.get(normalized_name)
        if existing_alias is None:
            aliases_to_create.append(ProductAlias(product=product, alias_name=alias_name))
            continue

        if existing_alias.alias_name != alias_name:
            existing_alias.alias_name = alias_name
            aliases_to_update.append(existing_alias)

    if aliases_to_create:
        ProductAlias.objects.bulk_create(aliases_to_create)

    if aliases_to_update:
        ProductAlias.objects.bulk_update(aliases_to_update, ["alias_name"])

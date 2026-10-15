# Homehub styling for Home Assistant

This is the exact styling surface used by HomeHub right now, with no placeholder or generated sample code.

## 1) Primary style assets (copy as-is)

Load these in this order to match HomeHub rendering behavior:

1. [static/output.css](static/output.css)
2. Font Awesome stylesheet from [templates/base.html](templates/base.html#L8)
3. Inter font stylesheet from [templates/base.html](templates/base.html#L11)
4. Config-driven theme variable block in [templates/base.html](templates/base.html#L14-L28)
5. Primary RGB variable style in [templates/base.html](templates/base.html#L30)
6. Base global style block in [templates/base.html](templates/base.html#L32-L93)
7. Page-specific style blocks listed below

## 2) Integrity manifest (current workspace state)

- [static/output.css](static/output.css) | lines: 2323 | sha256: 190361E2DD925ACDA6D2B57BF0B4D24B8AAC2AE3F5ED54F8D3802B01A36AB659
- [static/input.css](static/input.css) | lines: 481 | sha256: 2EE22D11F4B3F9966DE44AEF7AF644F7FB81BEB6EFEE41AA6E1D682B1BF6070D
- [static/quill/quill.snow.css](static/quill/quill.snow.css) | lines: 9 | sha256: 21840BE8A887550DCEBF3D48257FF4D9D4F73E3E02F8E20C292A955183A141DD
- [tailwind.config.js](tailwind.config.js) | lines: 94 | sha256: 8AF8F9683505E1F0D93F612C54301B2CE5BD12664325CA8D289FA59845AC89DD
- [templates/base.html](templates/base.html) | lines: 2842 | sha256: 3E1CD3F168378657E286F987C9B66A2CBCF2DE2F05E1D116A8863EE7793A4F1C
- [templates/calendar.html](templates/calendar.html) | lines: 1433 | sha256: B7937C9120B1C4D12A2F2272DFCF376BC68AC905E0946816216E89085F517100
- [templates/index.html](templates/index.html) | lines: 826 | sha256: FF301BED760F2405268CEF9E04BC414785227C4901F65321B1B9146556A1B1C4
- [templates/expenses.html](templates/expenses.html) | lines: 4758 | sha256: A1F2BE1B074334C12042723EFB9B255FCF9B2ED6A4304CBEFAECEBFFA1A04F4A
- [templates/meal_planner.html](templates/meal_planner.html) | lines: 1013 | sha256: 959BED567BF5CF3D615F9B8629540F8C86297D87DBB2B77FDD27BB5ED18B00FD
- [templates/money.html](templates/money.html) | lines: 429 | sha256: E45849C65475284C308ADE31CF16F641F287067BA24747BFD6B7C9FF8D90CCDB
- [templates/recipes.html](templates/recipes.html) | lines: 576 | sha256: 4A5CE6FCADC04CEA0E31575096C60969810BE1B507F45664942D8C400DAA826C
- [templates/settings.html](templates/settings.html) | lines: 1283 | sha256: FC3B4E76103151AE3ABB494660EB45891A78862998BA1B39F83930B4E2A2D67C

## 3) Page-level CSS blocks (copy verbatim)

These inline blocks are part of the live look-and-feel and must be included:

- Global theme/config/base UI styles:
  - [templates/base.html](templates/base.html#L14-L28)
  - [templates/base.html](templates/base.html#L30)
  - [templates/base.html](templates/base.html#L32-L93)
- Calendar UI-specific styles:
  - [templates/calendar.html](templates/calendar.html#L3-L67)
- Home dashboard styles:
  - [templates/index.html](templates/index.html#L6-L37)
- Expense tracker styles:
  - [templates/expenses.html](templates/expenses.html#L3-L75)
- Meal planner styles:
  - [templates/meal_planner.html](templates/meal_planner.html#L3-L21)
- Money module styles:
  - [templates/money.html](templates/money.html#L3-L47)
- Recipes module styles:
  - [templates/recipes.html](templates/recipes.html#L5-L29)
- Settings modal display style:
  - [templates/settings.html](templates/settings.html#L1341-L1345)

## 4) Tailwind source of truth

Tailwind utility generation for this app is defined by:

- [tailwind.config.js](tailwind.config.js)
- [static/input.css](static/input.css)

The compiled stylesheet actually used by HomeHub pages is:

- [static/output.css](static/output.css)

To match HomeHub exactly, use the compiled file plus all inline style blocks above.

## 5) Theme and behavior hooks required for visual parity

- Theme is controlled with data-theme on the root element, as used in [templates/base.html](templates/base.html#L144-L152).
- Sidebar, button, card, form, modal, and glassmorphism visuals are defined in [static/input.css](static/input.css) and reinforced by [templates/base.html](templates/base.html#L32-L93).
- Quill editor presentation depends on both [static/quill/quill.snow.css](static/quill/quill.snow.css) and local overrides in [static/input.css](static/input.css) and [templates/recipes.html](templates/recipes.html#L5-L29).

## 6) Exact transfer scope

If your Home Assistant implementation ingests the files and style blocks listed in sections 1 through 5 without modification, you will have the full HomeHub CSS style layer currently present in this repository.
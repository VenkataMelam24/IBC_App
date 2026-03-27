# IBC_App

IBC_App is a Django-based internal procurement and inventory management system built for Indian Biryani Company.

It helps manage products, vendors, vendor pricing, purchase flow, analytics, and bulk product onboarding through Excel import.

---

## Current Features

### Authentication
- Branded login/signup flow
- OTP-based auth flow structure
- Protected routes
- Forgot password flow
- Session-based access control

### Inventory / Master Inventory
- Manual product entry
- Manual vendor entry
- Manual vendor price entry
- Product listing and management
- Vendor pricing relationships

### Bulk Excel Import
Users can upload a `.xlsx` file to bulk-create and update:
- Products
- Vendors
- Vendor prices
- Alternative product names

Supported Excel columns:
- Product Name
- Display Name
- Packing Type
- Quantity
- Unit
- Vendor Name
- Price
- Alternative Names

Import logic includes:
- duplicate-safe product matching
- vendor reuse
- vendor price update/create
- quantity normalization
- row-level skip reasons
- import summary

### Analytics
- KPI cards
- vendor charts
- top products
- product price trend
- AJAX-based chart updates

### Purchasing Flow
- Product browsing
- cart flow
- purchase order flow
- history tracking

---

## Tech Stack

- Python
- Django
- PostgreSQL
- HTML / CSS / Django Templates
- JavaScript
- openpyxl for Excel import

---

## Project Structure

```bash
IBC_App/
├── accounts/        # Authentication, login/signup, OTP-related flows
├── analytics/       # Dashboard analytics and charts
├── carting/         # Cart and product order flow
├── config/          # Django project settings and URLs
├── inventory/       # Products, vendors, bulk import, aliases
├── pricing/         # Pricing-related logic
├── purchases/       # Purchase order and history flow
├── templates/       # Django templates
├── media/           # Uploaded product/media files (ignored in git)
└── manage.py

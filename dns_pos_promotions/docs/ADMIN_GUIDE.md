# DNS Promotions — Administrator Guide

## Daily workflow

Open **Point of Sale → Promotions → Promotion Workspace**. The dashboard separates active, scheduled, conflicted, ending-soon, and ended offers. Search accepts a promotion name, product name, primary barcode, or alternate barcode.

Create an offer in this order:

1. Enter the name, offer type, priority, and whether it is automatic.
2. Select All Stores, one or more reusable store groups, or individual POS configurations.
3. Add product selectors. A selector can use products, categories, brand, season, size, colour, vendor, or barcode. Multiple fields on one selector are combined.
4. Enter the reward and schedule. Dates are stored in UTC and displayed using the promotion timezone; Australia/Brisbane is the default.
5. Save and select **Validate**. Validation materializes product/store membership and performs conflict checks inside a database transaction.

A valid future offer becomes Scheduled; a currently valid offer becomes Active. Any overlapping product, store, and time window moves the proposed offer to Conflict. V1 deliberately has no override or stacking.

## Assortments

Mix-and-match applies a fixed total to any required quantity from the resolved eligible set. Multi-group offers require the configured quantity from every group. Use separate group selectors to express requirements such as “two drinks and one snack”.

## Retired offer types

**Order Total Fixed Discount** and **Coupon / Code Offer** are retired and no longer appear in the offer type picker. So are Single Product Bundle Price and Multi-group Assortment Bundle. Any promotion already stored on one of these types keeps its type, keeps pricing at the till, and can still have its dates, stores, and products edited — but no new promotion can be created on it, and an existing one cannot be moved onto it. The picker shows such a type marked *(retired)* while that promotion is open.

Use Order Total Percentage Discount for a basket-wide reward, and Product Fixed Amount Discount for a flat amount off a product.

## Product explorer and bulk actions

Use **Product explorer** to scan a primary or alternate barcode and see active, scheduled, and historical offers. The list supports selection and bulk archive. Product sets can also be imported by CSV from the promotion form; use a `barcode` column with one barcode per row.

Changing dates, stores, rewards, selectors, or assortment groups on a validated promotion returns it to Draft and requires validation again.

## Roles

- Promotion Manager: create, edit, validate, cancel, archive, migrate, and import.
- Promotion Backoffice: create, edit, clone and validate promotions that run only on tills in the user's
  **Allowed POS**. No All Stores, no Store Groups (neither using nor creating them), no Analysis, no import or
  migration. Network-wide promotions that reach their tills are visible read-only. Conflict checks still run
  against every live promotion, including ones the user cannot open.
- Promotion Viewer/POS User: read active, scheduled and ended offers that run on the tills in the user's
  **Allowed POS** (Settings > Users > Point of Sale tab). No Store Groups, no Analysis, no configuration RPC access.

Editing a store group's tills revalidates every active or scheduled promotion that uses it, so the change
reaches the tills immediately. The edit is refused if it would leave such a promotion with no stores or put it
in conflict with another live promotion.


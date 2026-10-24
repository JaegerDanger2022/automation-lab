-- Internal price amendments carry the new unit price, so the payable calculator can apply
-- them in code instead of leaving the model to interpret the wording.
ALTER TABLE vendor_documents ADD COLUMN IF NOT EXISTS unit_price numeric(12, 2);
UPDATE vendor_documents SET unit_price = 50.40
 WHERE doc_type = 'price_amendment' AND sku = 'LED-18' AND unit_price IS NULL;

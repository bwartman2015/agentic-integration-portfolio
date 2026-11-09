# Big Box Outdoor X12 850 implementation guide (synthetic)

## BEG segment
BEG01 = 00 (original). BEG03 = PO number, max 10 characters. BEG05 = PO date CCYYMMDD.

## N1 ship-to loop
N1*ST with N104 = 4-digit store or DC number. New ship-to numbers must be confirmed against the store list before acknowledging.

## PO1 line items
PO102 = quantity, PO103 = UOM (EA or CA; one case = 12 each). PO104 = unit price, must match the price agreement within $0.01. PO107 = retailer item number; PO109 = vendor (Ridgeline) SKU.

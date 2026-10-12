"""Printable car parts vs. eBay: the savings pipeline behind savings-review.

Thingiverse Thing -> classify (GPT) -> eBay search -> strict matcher -> price
judge -> OrcaSlicer print cost -> savings per print x downloads -> review site.
"""

__version__ = "1.0.0"

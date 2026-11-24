"""Specimens: projects generated from a model passport.

A specimen is built constructively, without a PRNG, from a `kind: specimen` preset that states a
passport's leaves. Every stated leaf is met exactly or refused by its dotted path with a closed
class; nothing is approximated.
"""

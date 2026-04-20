from canopy_core.compat import alias_module

_MODULE = alias_module(__name__, "canopy_core.stage_kernel.detailed_design")


if __name__ == "__main__":
    raise SystemExit(_MODULE.main())

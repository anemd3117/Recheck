import sys

if __name__ == '__main__':
    if '--cli' in sys.argv[1:]:
        from recheck.cli import main

        args = sys.argv[1:]
        args.remove('--cli')
        raise SystemExit(main(args))
    from recheck.gui import main

    raise SystemExit(main())

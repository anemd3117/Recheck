#include <errno.h>
#include <stdio.h>
#include <stdlib.h>

static int read_units(int argc, char **argv, long *value) {
    char input[128], *end;
    const char *text;
    if (argc == 2) text = argv[1];
    else if (argc == 1 && fgets(input, sizeof(input), stdin)) text = input;
    else return 0;
    errno = 0;
    *value = strtol(text, &end, 10);
    if (errno != 0 || end == text) return 0;
    while (*end == ' ' || *end == '\n' || *end == '\r' || *end == '\t') ++end;
    return errno == 0 && end != text && *end == 0 && *value >= 0 && *value <= 1000;
}

static long calculate_fee(long units) {
    if (units >= 50) return units * 2;
    return units * 3;
}

int main(int argc, char **argv) {
    long units;
    if (!read_units(argc, argv, &units)) {
        fputs("error: expected one integer in [0,1000]\n", stderr);
        return 2;
    }
    printf("units=%ld\nfee=%ld\n", units, calculate_fee(units));
    return 0;
}

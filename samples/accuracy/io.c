#include <stdio.h>
#include <stdlib.h>
#ifndef MUTANT
#define MUTANT 0
#endif
#ifndef ALTERNATE
#define ALTERNATE 0
#endif
int main(int argc, char **argv) {
    long long n, extra, delta; char *end;
    if (argc != 3) { fputs("usage: file delta\n", stderr); return 3; }
    delta = strtoll(argv[2], &end, 10);
    if (*end || delta < -10000 || delta > 10000) {
        fputs("invalid delta\n", stderr); return 4;
    }
    FILE *file = fopen(argv[1], "rb");
    if (!file) { fputs("missing input file\n", stderr); return 5; }
    int ok = fscanf(file, "%lld", &extra); fclose(file);
    if (ok != 1 || extra < -10000 || extra > 10000) {
        fputs("invalid file\n", stderr); return 6;
    }
    if (scanf("%lld", &n) != 1 || n < -10000 || n > 10000) {
        fputs("invalid stdin\n", stderr); return 2;
    }
    long long result = ALTERNATE ? delta + extra + n : n + extra + delta;
    if (MUTANT) result -= delta;
    printf("%lld\n", result); return 0;
}

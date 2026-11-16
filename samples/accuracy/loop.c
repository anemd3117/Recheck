#include <stdio.h>
#ifndef MUTANT
#define MUTANT 0
#endif
#ifndef ALTERNATE
#define ALTERNATE 0
#endif
int main(void) {
    int n; long long sum = 0;
    if (scanf("%d", &n) != 1 || n < 0 || n > 10000) {
        fputs("invalid count\n", stderr); return 2;
    }
    if (ALTERNATE) sum = (long long)n * (n + 1) / 2;
    else for (int i = 1; MUTANT ? i < n : i <= n; ++i) sum += i;
    printf("%lld\n", sum); return 0;
}

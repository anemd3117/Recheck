#include <stdio.h>
#ifndef MUTANT
#define MUTANT 0
#endif
#ifndef ALTERNATE
#define ALTERNATE 0
#endif
int main(void) {
    long long n;
    if (scanf("%lld", &n) != 1 || n < -10000 || n > 10000) {
        fputs("invalid integer\n", stderr); return 2;
    }
    long long result = ALTERNATE ? n * (n + 3) - 2 : n * n + 3 * n - 2;
    if (MUTANT && n == 0) result++;
    printf("%lld\n", result); return 0;
}

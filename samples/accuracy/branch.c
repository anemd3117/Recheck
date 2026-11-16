#include <stdio.h>
#ifndef MUTANT
#define MUTANT 0
#endif
#ifndef ALTERNATE
#define ALTERNATE 0
#endif
int main(void) {
    int n;
    if (scanf("%d", &n) != 1 || n < 0 || n > 10000) {
        fputs("invalid quantity\n", stderr); return 2;
    }
    int discounted = MUTANT ? n < 100 : n <= 100;
    int value = ALTERNATE ? 1000 - 100 * discounted : (discounted ? 900 : 1000);
    printf("%d\n", value); return 0;
}

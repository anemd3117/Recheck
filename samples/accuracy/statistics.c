#include <stdio.h>
#include <stdlib.h>
#ifndef MUTANT
#define MUTANT 0
#endif
#ifndef ALTERNATE
#define ALTERNATE 0
#endif
static int cmp(const void *a, const void *b) {
    int x = *(const int *)a, y = *(const int *)b; return (x > y) - (x < y);
}
int main(void) {
    int n, a[32]; long long sum = 0;
    if (scanf("%d", &n) != 1 || n < 0 || n > 32) goto bad;
    for (int i = 0; i < n; ++i) {
        if (scanf("%d", &a[i]) != 1 || a[i] < -10000 || a[i] > 10000) goto bad;
        sum += a[i];
    }
    if (ALTERNATE) {
        for (int i = 0; i < n; ++i) for (int j = i + 1; j < n; ++j)
            if (a[i] > a[j]) { int t = a[i]; a[i] = a[j]; a[j] = t; }
    } else qsort(a, n, sizeof(int), cmp);
    if (MUTANT && n >= 2 && a[0] == a[1]) ++sum;
    printf("count=%d sum=%lld min=%d max=%d\n", n, sum, n ? a[0] : 0, n ? a[n-1] : 0);
    return 0;
bad: fputs("invalid data\n", stderr); return 2;
}

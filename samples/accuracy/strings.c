#include <stdio.h>
#include <string.h>
#ifndef MUTANT
#define MUTANT 0
#endif
#ifndef ALTERNATE
#define ALTERNATE 0
#endif
int main(void) {
    char text[1024] = {0};
    if (!fgets(text, sizeof text, stdin)) text[0] = 0;
    size_t n = strlen(text);
    if (n && text[n - 1] == '\n') text[--n] = 0;
    if (ALTERNATE) { n = 0; while (text[n]) ++n; }
    if (MUTANT && n == 0) ++n;
    printf("bytes=%zu\n", n); return 0;
}

// load HOST PORT CONNS TOTAL RATE PATH HEADERS_FILE BODY_FILE OUT_FILE
// POSTs TOTAL requests over CONNS keep-alive connections, one request in flight per connection. The body is BODY_FILE with its first run of ten '@' replaced
// by the request's number n (1..TOTAL), zero padded to ten digits, so every body has the same length. HEADERS_FILE holds extra header lines (e.g. an
// Authorization), each ending in \r\n or \n. RATE 0 is a closed loop (as fast as the answers come); RATE > 0 is an open loop: request n is due at
// start + (n-1)/RATE and is sent when a connection is free, and its latency counts from when it was due (so a slow service does not hide its queue).
// OUT_FILE gets one line per request: "n t_due t_send t_ack status" (CLOCK_MONOTONIC ns, the sink's clock). A status other than 2xx counts as a failure.
// Prints: "N requests, C conns, S s, R req/s, ack p50 A ms, ack p99 B ms, non-2xx F".
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/epoll.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>
typedef struct { int fd; char buf[8192]; int n; long seq; } C;
typedef struct { int64_t due, send, ack; int status; } L;
static int64_t now(void){ struct timespec t; clock_gettime(CLOCK_MONOTONIC,&t); return (int64_t)t.tv_sec*1000000000LL+t.tv_nsec; }
static int cmp(const void*a,const void*b){ int64_t x=*(int64_t*)a,y=*(int64_t*)b; return x<y?-1:x>y; }
static char *slurp(const char*p,long*n){ FILE*f=fopen(p,"rb"); if(!f){ perror(p); exit(1);} fseek(f,0,SEEK_END); *n=ftell(f); rewind(f); char*b=malloc(*n+1); if(fread(b,1,*n,f)!=(size_t)*n){ perror("read"); exit(1);} b[*n]=0; fclose(f); return b; }
int main(int argc,char**argv){
  if(argc<10){ fprintf(stderr,"usage: load HOST PORT CONNS TOTAL RATE PATH HEADERS_FILE BODY_FILE OUT_FILE\n"); return 1; }
  const char*host=argv[1]; int port=atoi(argv[2]), conns=atoi(argv[3]); long total=atol(argv[4]); double rate=atof(argv[5]); const char*path=argv[6];
  long hl, bl; char*hdrs=slurp(argv[7],&hl); char*body=slurp(argv[8],&bl);
  char*at=strstr(body,"@@@@@@@@@@"); if(!at){ fprintf(stderr,"no ten @ in the body file\n"); return 1; }
  // normalise the header lines to \r\n
  char hbuf[4096]; int ho=0; for(long i=0;i<hl&&ho<4000;i++){ if(hdrs[i]=='\r')continue; if(hdrs[i]=='\n'){ hbuf[ho++]='\r'; hbuf[ho++]='\n'; } else hbuf[ho++]=hdrs[i]; } hbuf[ho]=0;
  char head[6144]; int hn=snprintf(head,sizeof head,"POST %s HTTP/1.1\r\nHost: %s:%d\r\nContent-Type: application/json\r\nContent-Length: %ld\r\n%s\r\n",path,host,port,bl,hbuf);
  int ep=epoll_create1(0); C*cs=calloc(conns,sizeof(C)); L*lg=calloc(total+1,sizeof(L));
  struct sockaddr_in a={.sin_family=AF_INET,.sin_port=htons(port)}; inet_pton(AF_INET,host,&a.sin_addr);
  int *idle=malloc(sizeof(int)*conns), nidle=0; long next=1, done=0, fails=0; int64_t start=now()+50000000LL;
  for(int i=0;i<conns;i++){ int fd=socket(AF_INET,SOCK_STREAM,0); if(connect(fd,(void*)&a,sizeof a)){ perror("connect"); return 1; } int one=1; setsockopt(fd,IPPROTO_TCP,TCP_NODELAY,&one,sizeof one);
    fcntl(fd,F_SETFL,O_NONBLOCK); cs[i].fd=fd; struct epoll_event e={.events=EPOLLIN,.data.ptr=&cs[i]}; epoll_ctl(ep,EPOLL_CTL_ADD,fd,&e); idle[nidle++]=i; }
  char msg[16384];
  #define SEND(c,sq) do{ long _s=(sq); memcpy(msg,head,hn); memcpy(msg+hn,body,bl); char dg[16]; snprintf(dg,sizeof dg,"%010ld",_s); memcpy(msg+hn+(at-body),dg,10); \
    lg[_s].send=now(); (c)->seq=_s; if(write((c)->fd,msg,hn+bl)!=hn+bl){ perror("write"); exit(1);} }while(0)
  struct epoll_event evs[256];
  while(done<total){
    // send what is due and can be sent
    while(nidle>0 && next<=total){ int64_t due= rate>0 ? start+(int64_t)((next-1)*1e9/rate) : 0; int64_t t=now(); if(rate>0 && t<due)break; if(rate==0)due=t; lg[next].due=due; C*c=&cs[idle[--nidle]]; SEND(c,next); next++; }
    int to=5000; if(nidle>0 && next<=total && rate>0){ int64_t due=start+(int64_t)((next-1)*1e9/rate); int64_t w=(due-now())/1000000; to= w<=0?0:(int)(w>5000?5000:w); }
    int k=epoll_wait(ep,evs,256,to); if(k==0 && to==5000){ fprintf(stderr,"timeout done=%ld\n",done); return 2; }
    for(int j=0;j<k;j++){ C*c=evs[j].data.ptr; int r=read(c->fd,c->buf+c->n,sizeof(c->buf)-c->n-1); if(r<=0){ if(r<0&&errno==EAGAIN)continue; fprintf(stderr,"closed by the service after %ld answers\n",done); return 3; } c->n+=r; c->buf[c->n]=0;
      char*h=strstr(c->buf,"\r\n\r\n"); if(!h)continue; if(strcasestr(c->buf,"transfer-encoding: chunked")){ fprintf(stderr,"chunked answer: not supported by this tool\n"); return 4; }
      int cl=0; char*p=strcasestr(c->buf,"content-length:"); if(p&&p<h)cl=atoi(p+15); if(c->n<(h-c->buf)+4+cl)continue;
      int st=atoi(c->buf+9); lg[c->seq].ack=now(); lg[c->seq].status=st; if(st<200||st>=300)fails++; done++; c->n=0; idle[nidle++]=(int)(c-cs); } }
  int64_t end=now(); double el=(end-lg[1].send)/1e9;
  int64_t *d=malloc(sizeof(int64_t)*total); for(long i=1;i<=total;i++) d[i-1]=lg[i].ack-lg[i].due; qsort(d,total,sizeof(int64_t),cmp);
  FILE*o=fopen(argv[9],"w"); for(long i=1;i<=total;i++) fprintf(o,"%ld %lld %lld %lld %d\n",i,(long long)lg[i].due,(long long)lg[i].send,(long long)lg[i].ack,lg[i].status); fclose(o);
  printf("%ld requests, %d conns, %.2f s, %.0f req/s, ack p50 %.2f ms, ack p99 %.2f ms, non-2xx %ld\n",total,conns,el,total/el,d[total/2]/1e6,d[(long)(total*0.99)]/1e6,fails); return 0; }

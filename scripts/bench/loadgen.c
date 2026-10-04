// loadgen port conns total [bodybytes]: POST /events over keep-alive connections, one request in flight per connection.
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/epoll.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>
typedef struct { int fd; char buf[4096]; int n; double t0; } C;
static double now(void){ struct timespec t; clock_gettime(CLOCK_MONOTONIC,&t); return t.tv_sec+t.tv_nsec*1e-9; }
static int cmp(const void*a,const void*b){ double x=*(double*)a,y=*(double*)b; return x<y?-1:x>y; }
int main(int argc,char**argv){
  int port=atoi(argv[1]), conns=atoi(argv[2]); long total=atol(argv[3]); int body=argc>4?atoi(argv[4]):200;
  char payload[4096]; int pl=0; pl+=sprintf(payload,"{\"type\":\"bench\",\"n\":0,\"pad\":\""); while(pl<body-2) payload[pl++]='x'; pl+=sprintf(payload+pl,"\"}");
  char req[8192]; int rl=sprintf(req,"POST /events HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n%.*s",pl,pl,payload);
  int ep=epoll_create1(0); C*cs=calloc(conns,sizeof(C)); double*lat=malloc(sizeof(double)*total); long sent=0,done=0,fails=0;
  struct sockaddr_in a={.sin_family=AF_INET,.sin_port=htons(port)}; inet_pton(AF_INET,"127.0.0.1",&a.sin_addr);
  double start=now();
  for(int i=0;i<conns;i++){ int fd=socket(AF_INET,SOCK_STREAM,0); if(connect(fd,(void*)&a,sizeof a)){perror("connect");return 1;} int one=1; setsockopt(fd,IPPROTO_TCP,TCP_NODELAY,&one,sizeof one);
    fcntl(fd,F_SETFL,O_NONBLOCK); cs[i].fd=fd; struct epoll_event e={.events=EPOLLIN,.data.ptr=&cs[i]}; epoll_ctl(ep,EPOLL_CTL_ADD,fd,&e);
    if(sent<total){ cs[i].t0=now(); if(write(fd,req,rl)!=rl){perror("write");return 1;} sent++; } }
  struct epoll_event evs[256];
  while(done<total){ int k=epoll_wait(ep,evs,256,5000); if(k==0){fprintf(stderr,"timeout done=%ld\n",done);return 2;}
    for(int j=0;j<k;j++){ C*c=evs[j].data.ptr; int r=read(c->fd,c->buf+c->n,sizeof(c->buf)-c->n-1); if(r<=0){ if(r<0&&errno==EAGAIN)continue; fprintf(stderr,"closed\n"); return 3;} c->n+=r; c->buf[c->n]=0;
      char*h=strstr(c->buf,"\r\n\r\n"); if(!h)continue; int cl=0; char*p=strcasestr(c->buf,"content-length:"); if(p)cl=atoi(p+15); if(c->n<(h-c->buf)+4+cl)continue;
      if(strncmp(c->buf,"HTTP/1.1 202",12)) fails++; lat[done++]=now()-c->t0; c->n=0;
      if(sent<total){ c->t0=now(); if(write(c->fd,req,rl)!=rl){perror("write");return 1;} sent++; } } }
  double el=now()-start; qsort(lat,total,sizeof(double),cmp);
  printf("%ld requests, %d conns, %.2fs, %.0f req/s, p50 %.2f ms, p99 %.2f ms, non-202 %ld\n",total,conns,el,total/el,lat[total/2]*1e3,lat[(long)(total*0.99)]*1e3,fails); return 0; }

// sink BASE_PORT NPORTS CAPACITY: a receiver for NPORTS endpoints (BASE_PORT .. BASE_PORT+NPORTS-1) that answers every POST 204, on keep-alive
// connections unless the request says `Connection: close`, and records for each request the endpoint (the port), the number n (the first `"n":<digits>` or `"n":"<digits>"`
// of the body) and the arrival time (CLOCK_MONOTONIC, ns). The control port is BASE_PORT+NPORTS:
//   GET /count -> the number of requests;  GET /dump -> one line per request, "endpoint n t_ns";  GET /reset -> forget everything.
// A request this cannot frame (chunked) gets a 400, so that it shows up as a failed delivery rather than as a silent miscount.
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/epoll.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>
typedef struct { int fd; int idx; int ctl; char buf[65536]; int n; } C;
typedef struct { int ep; long n; int64_t t; } R;
static R *recs; static long nrec, cap;
static int64_t now(void){ struct timespec t; clock_gettime(CLOCK_MONOTONIC,&t); return (int64_t)t.tv_sec*1000000000LL+t.tv_nsec; }
static void close_c(int ep, C*c){ epoll_ctl(ep,EPOLL_CTL_DEL,c->fd,0); close(c->fd); free(c); }
static void send_all(int fd,const char*b,size_t n){ while(n){ ssize_t w=write(fd,b,n); if(w<0){ if(errno==EAGAIN||errno==EINTR){ usleep(100); continue;} return;} b+=w; n-=w; } }
int main(int argc,char**argv){
  if(argc<4){ fprintf(stderr,"usage: sink BASE_PORT NPORTS CAPACITY\n"); return 1; }
  int base=atoi(argv[1]), np=atoi(argv[2]); cap=atol(argv[3]); recs=malloc(sizeof(R)*cap);
  signal(SIGPIPE,SIG_IGN);
  int ep=epoll_create1(0); int lfd[256]; if(np>200){ fprintf(stderr,"at most 200 ports\n"); return 1; }
  for(int i=0;i<=np;i++){ int l=socket(AF_INET,SOCK_STREAM,0); int one=1; setsockopt(l,SOL_SOCKET,SO_REUSEADDR,&one,sizeof one);
    struct sockaddr_in a={.sin_family=AF_INET,.sin_port=htons(base+i)}; a.sin_addr.s_addr=htonl(INADDR_LOOPBACK);
    if(bind(l,(void*)&a,sizeof a)||listen(l,1024)){ perror("bind"); return 1; }
    fcntl(l,F_SETFL,O_NONBLOCK); lfd[i]=l; struct epoll_event e={.events=EPOLLIN,.data.u64=(uint64_t)(i+1)}; epoll_ctl(ep,EPOLL_CTL_ADD,l,&e); }
  fprintf(stderr,"listening\n");
  struct epoll_event evs[256];
  for(;;){ int k=epoll_wait(ep,evs,256,-1); for(int j=0;j<k;j++){
    if(evs[j].data.u64<=(uint64_t)(np+1)){ int i=(int)evs[j].data.u64-1;
      for(;;){ int fd=accept(lfd[i],0,0); if(fd<0)break; fcntl(fd,F_SETFL,O_NONBLOCK); int one=1; setsockopt(fd,IPPROTO_TCP,TCP_NODELAY,&one,sizeof one);
        C*n=calloc(1,sizeof(C)); n->fd=fd; n->idx=i; n->ctl=(i==np); struct epoll_event ne={.events=EPOLLIN,.data.ptr=n}; epoll_ctl(ep,EPOLL_CTL_ADD,fd,&ne); }
      continue; }
    C*c=evs[j].data.ptr; int64_t t=now();
    for(;;){ int r=read(c->fd,c->buf+c->n,sizeof(c->buf)-c->n-1); if(r==0){ close_c(ep,c); c=0; break; } if(r<0){ if(errno==EAGAIN)break; if(errno==EINTR)continue; close_c(ep,c); c=0; break; } c->n+=r; if(c->n>=(int)sizeof(c->buf)-1)break; }
    if(!c) continue;
    for(;;){ c->buf[c->n]=0; char*h=strstr(c->buf,"\r\n\r\n"); if(!h)break; int hl=(h-c->buf)+4;
      char*te=strcasestr(c->buf,"transfer-encoding:"); if(te&&te<h){ send_all(c->fd,"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",64); close_c(ep,c); c=0; break; }
      int cl=0; char*p=strcasestr(c->buf,"content-length:"); if(p&&p<h)cl=atoi(p+15); if(c->n<hl+cl)break;
      int close_after=0; char*cn=strcasestr(c->buf,"connection:"); if(cn&&cn<h){ char*e=strstr(cn,"\r\n"); if(strcasestr(cn,"close")&&strcasestr(cn,"close")<e)close_after=1; }
      if(c->ctl){ char out[128]; char*body=0; long len=0;
        if(!strncmp(c->buf,"GET /count",10)){ len=snprintf(out,sizeof out,"%ld\n",nrec); body=out; }
        else if(!strncmp(c->buf,"GET /reset",10)){ nrec=0; len=snprintf(out,sizeof out,"ok\n"); body=out; }
        else if(!strncmp(c->buf,"GET /dump",9)){ size_t sz=nrec*40+16; char*d=malloc(sz); size_t o=0; for(long i=0;i<nrec;i++) o+=snprintf(d+o,sz-o,"%d %ld %lld\n",recs[i].ep,recs[i].n,(long long)recs[i].t);
          char hd[128]; int m=snprintf(hd,sizeof hd,"HTTP/1.1 200 OK\r\nContent-Length: %zu\r\n\r\n",o); send_all(c->fd,hd,m); send_all(c->fd,d,o); free(d); body=0; len=-1; }
        if(len>=0){ char hd[160]; int m=snprintf(hd,sizeof hd,"HTTP/1.1 200 OK\r\nContent-Length: %ld\r\n\r\n",len); send_all(c->fd,hd,m); if(body)send_all(c->fd,body,len); }
        close_after=1; }
      else { char*b=c->buf+hl; long nn=-1; char*q=memmem(b,cl,"\"n\":",4); if(q){ q+=4; while(*q==' '||*q=='"')q++; nn=strtol(q,0,10); }
        if(nrec<cap){ recs[nrec].ep=c->idx; recs[nrec].n=nn; recs[nrec].t=t; nrec++; }
        const char*resp=close_after?"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\nConnection: close\r\n\r\n":"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n";
        send_all(c->fd,resp,strlen(resp)); }
      if(close_after){ close_c(ep,c); c=0; break; }
      memmove(c->buf,c->buf+hl+cl,c->n-hl-cl); c->n-=hl+cl; }
  } }
}
